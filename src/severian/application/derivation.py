from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from severian.application.anticipation import anticipation_enabled, extract_trigger
from severian.application.datestamps import date_stamps_enabled, dated
from severian.application.eventtime import event_time_enabled, extract_event_span, message_time
from severian.application.extraction import EntityExtractionService
from severian.application.factanchors import grounded
from severian.application.ports import (
    CanonicalRepository,
)
from severian.application.ports import (
    DerivationGateway as DerivationGateway,
)
from severian.application.truthresolution import TruthResolver
from severian.application.userfacts import FACT_KIND, PROMPT_VERSION, Extraction, time_label, user_text
from severian.domain.errors import NotFoundError
from severian.domain.models import Memory, Observation, Scope

# ``DerivationGateway`` lives in application.ports (single source of
# truth); re-export here so existing imports keep working while
# consumers migrate to the canonical home.

__all__ = ["CloudPolicyError", "DerivationGateway", "DerivationService"]

logger = logging.getLogger(__name__)


class FactSource(Protocol):
    """Round 2 idea 1: turns user messages into facts (``userfacts.FactExtractor`` in production)."""

    model: str

    def extract(self, messages: list[str]) -> Extraction: ...


@dataclass(frozen=True)
class UserFacts:
    """What one fact batch gave: the facts published, and the turns given up on (observation id -> why)."""

    published: list[Memory] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)


class CloudPolicyError(PermissionError):
    pass


class DerivationService:
    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        extraction: EntityExtractionService | None = None,
        event_time: bool | None = None,
        date_stamps: bool | None = None,
        anticipation: bool | None = None,
        cue_generator: Callable[[str], list[str]] | None = None,
        fact_extractor: FactSource | None = None,
        truth_resolver: TruthResolver | None = None,
    ) -> None:
        self.repository = repository
        # Handoff 1 (SEVERIAN_TRUTH_RESOLUTION; off by default): after memories are saved, close the ones a
        # newer fact replaces. Only the worker's derivation gets one; it never deletes and never raises.
        self._truth_resolver = truth_resolver
        # Step 3: find "when it happened" in each derived memory (SEVERIAN_EVENT_TIME; off by default).
        self._event_time = event_time_enabled() if event_time is None else event_time
        # Round 2 improvement 2 (SEVERIAN_DATE_STAMPS; off by default): each memory's text carries its date and
        # weekday, and relative dates get the real date in brackets. Event time, cues and dedupe still read
        # the text as derived, so turning this on changes only what is stored and shown.
        self._date_stamps = date_stamps_enabled() if date_stamps is None else date_stamps
        # Step 4: triggers for instructions-for-later (rules) and cues (one background model call per
        # memory). SEVERIAN_ANTICIPATION, off by default. A cue failure never fails the derive.
        self._anticipation = anticipation_enabled() if anticipation is None else anticipation
        self._cue_generator = cue_generator
        # Round 2 idea 1 (SEVERIAN_USER_FACTS): the background fact pass's model. Only the worker's
        # derivation gets one; derive_user_facts does nothing without it.
        self._fact_extractor = fact_extractor
        # Optional deterministic graph extractor (P1c). When provided,
        # every derive pass also upserts entities/relations so the
        # graph store stays populated without manual seeding.
        self._extraction = extraction

    @property
    def truth_resolver(self) -> TruthResolver | None:
        return self._truth_resolver

    def _resolve(self, scope: Scope, published: Sequence[Memory]) -> None:
        if self._truth_resolver is not None and published:
            self._truth_resolver.resolve(scope, published)

    @property
    def fact_extractor(self) -> FactSource | None:
        """The background fact pass's model (Round 2 idea 1); its call and token counters are the cost record."""
        return self._fact_extractor

    def _effective_policy(self, scope: Scope, caller_policy: dict[str, object]) -> dict[str, object]:
        """Merge canonical collection policy with caller hints (deny-dominant).

        The stored collection policy is the source of truth. A caller
        may only TIGHTEN it, never loosen it: security-sensitive keys
        (``cloud_allowed``) are forced to their most restrictive value.

        A missing collection fails closed: there is no stored policy to
        prove cloud is permitted, so ``cloud_allowed`` stays unset
        (cloud derivation is denied). Local derivation is unaffected.
        """
        stored: dict[str, object] = {}
        try:
            collection = self.repository.get_collection(scope.tenant_id, scope.collection_id)
            stored = dict(collection.policy or {})
        except NotFoundError:
            # No stored collection: fail closed. There is no policy to
            # prove cloud is permitted, so ``cloud_allowed`` stays unset
            # (cloud derivation is denied). Local derivation proceeds
            # with an empty effective policy.
            stored = {}
        merged = dict(stored)
        for key, value in caller_policy.items():
            if key == "cloud_allowed":
                # Deny-dominant: False anywhere means False overall.
                if value is False:
                    merged[key] = False
                continue
            if key not in merged:
                merged[key] = value
        return merged

    def derive_observation(
        self,
        scope: Scope,
        observation_id: str,
        gateway: DerivationGateway,
        *,
        policy: dict[str, object] | None = None,
    ) -> list[Memory]:
        observation = self.repository.get_observation(scope, observation_id)
        # A superseded observation must never produce a NEW active memory
        # (P0-5 correction propagation). The correction path tombstones
        # memories already derived from it; the guard here prevents the
        # queued derive job for the old head from resurrecting it after
        # the revision lands (a stale-fact gate failure otherwise).
        if observation.superseded_by_id is not None:
            return []
        effective = self._effective_policy(scope, dict(policy or {}))
        if getattr(gateway, "is_cloud", False) and effective.get("cloud_allowed") is not True:
            raise CloudPolicyError("cloud derivation is forbidden by collection policy")

        candidates = gateway.derive(observation.content, policy=effective)
        # Deterministic graph extraction (P1c): every derived observation
        # also populates the Entity/Relation store so the graph retrieval
        # service has data without manual seeding. Extraction is
        # rule-based and reproducible — no LLM, no randomness.
        # DATA-04: extraction and memory publication are ONE atomic
        # transaction via ``publish_derivation`` — the graph can never be
        # orphaned from the memory it produced (or vice versa).
        extraction_service = getattr(self, "_extraction", None)
        extracted_entities: list[tuple[str, str]] = []
        extracted_relations: list[tuple[str, str, str]] = []
        if extraction_service is not None:
            result = extraction_service.extract(scope, observation.content)
            extracted_entities = result.entities
            extracted_relations = result.relations
        # Strict candidate validation BEFORE any write: a malformed
        # candidate aborts the whole batch — no partial publication
        # (P0-4). Provenance comes from the TRUSTED gateway adapter,
        # never from model-produced candidates (P0-2): a candidate
        # cannot forge model_id / prompt_version.
        model_id = str(getattr(gateway, "model_id", "unknown"))
        prompt_version = str(getattr(gateway, "prompt_version", "unknown"))
        validated: list[Memory] = []
        seen: set[tuple[str, str]] = set()
        # Revision-observation guard (C-08): a revision observation
        # (created by the correction path) carries the SAME content as
        # the superseding observation's own memory. Deriving it creates
        # a duplicate memory with identical content that ties on every
        # stable ranking key and falls through to the random memory
        # UUID — non-deterministic across fresh adapters. The
        # superseding observation (active, non-revision) already
        # produces this content's memory; skip the redundant revision
        # derivation so exactly ONE memory exists per fact.
        if observation.revision_of_id is not None:
            revision_content_covered: set[str] = {
                obs.content
                for obs in self.repository.list_observations(scope)
                if obs.superseded_by_id is None and obs.revision_of_id is None
            }
        else:
            revision_content_covered = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError(f"malformed derivation candidate: {candidate!r}")
            kind = str(candidate.get("kind", "fact"))
            content = str(candidate.get("content", "")).strip()
            if not content:
                raise ValueError("derivation candidate content must not be empty")
            if observation.revision_of_id is not None and content in revision_content_covered:
                continue
            identity = (kind, content)
            if identity in seen:
                continue
            seen.add(identity)
            span = (
                extract_event_span(content, message_time(observation.source, observation.created_at))
                if self._event_time
                else None
            )
            validated.append(
                Memory(
                    occurred_at=span[0] if span else None,
                    occurred_until=span[1] if span else None,
                    cues=self._cues_for(content),
                    trigger=extract_trigger(content) if self._anticipation else None,
                    scope=scope,
                    kind=kind,
                    content=self._dated(content, observation),
                    source_observation_ids=[observation.id],
                    model_id=model_id,
                    prompt_version=prompt_version,
                    confidence=float(candidate.get("confidence", 0.5)),
                )
            )
        published = self.repository.publish_derivation(
            scope,
            entities=extracted_entities,
            relations=extracted_relations,
            memories=validated,
            source_observation_id=observation.id,
        )
        self._resolve(scope, published)
        return published

    def _cues_for(self, content: str) -> list[str]:
        """Cues for one memory: ``[]`` when anticipation is off, no generator is set, or it fails."""
        if not self._anticipation or self._cue_generator is None:
            return []
        try:
            return list(self._cue_generator(content))
        except Exception:  # cues are an extra; the memory must still be written
            return []

    def _dated(self, content: str, observation: Observation) -> str:
        if not self._date_stamps:
            return content
        return dated(content, message_time(observation.source, observation.created_at))

    def derive_user_facts(self, scope: Scope, observation_ids: Sequence[str]) -> UserFacts:
        """Facts the user stated in these turns, from ONE batch to the model (Round 2 idea 1).

        Turns that are gone, superseded or hold nothing from the user are skipped. Each fact is a
        ``user_fact`` memory with the same source turn and time label as the turn's own memory, so
        the raw turn stays linked. Turns the model could not serve come back in ``failed`` with the
        reason; their raw turns are already saved and searchable.
        """
        if self._fact_extractor is None:
            return UserFacts()
        turns: list[tuple[str, Observation, str]] = []
        for observation_id in observation_ids:
            try:
                observation = self.repository.get_observation(scope, observation_id)
            except NotFoundError:
                continue
            said = None if observation.superseded_by_id is not None else user_text(
                observation.content, observation.source
            )
            if said:
                turns.append((said, observation, time_label(observation.content)))
        if not turns:
            return UserFacts()
        try:
            found = self._fact_extractor.extract([said for said, _, _ in turns])
        except Exception as exc:  # the production extractor never raises; facts stay optional either way
            logger.warning("fact extraction raised; no facts for %d turns", len(turns), exc_info=True)
            reason = f"fact extraction raised {type(exc).__name__}: {exc}"
            return UserFacts(failed={observation.id: reason for _, observation, _ in turns})
        model_id = str(getattr(self._fact_extractor, "model", "unknown"))
        published: list[Memory] = []
        for number, (said, observation, label) in enumerate(turns, 1):
            memories = []
            for fact in found.facts.get(number, []):
                if not grounded(fact, said):  # misattached specifics or pasted text: never stored
                    continue
                content = f"{label} {fact}" if label else fact
                span = (
                    extract_event_span(content, message_time(observation.source, observation.created_at))
                    if self._event_time
                    else None
                )
                memories.append(
                    Memory(
                        occurred_at=span[0] if span else None,
                        occurred_until=span[1] if span else None,
                        scope=scope,
                        kind=FACT_KIND,
                        content=self._dated(content, observation),
                        source_observation_ids=[observation.id],
                        model_id=model_id,
                        prompt_version=PROMPT_VERSION,
                        confidence=0.5,
                    )
                )
            if memories:
                saved = self.repository.publish_derivation(
                    scope, entities=[], relations=[], memories=memories, source_observation_id=observation.id
                )
                published += saved
                self._resolve(scope, saved)
        failed = {turns[number - 1][1].id: why for number, why in found.failed.items() if 1 <= number <= len(turns)}
        return UserFacts(published=published, failed=failed)
