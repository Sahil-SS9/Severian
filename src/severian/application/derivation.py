from __future__ import annotations

from severian.application.eventtime import event_time_enabled, extract_event_span, message_time
from severian.application.extraction import EntityExtractionService
from severian.application.ports import (
    CanonicalRepository,
)
from severian.application.ports import (
    DerivationGateway as DerivationGateway,
)
from severian.domain.errors import NotFoundError
from severian.domain.models import Memory, Scope

# ``DerivationGateway`` lives in application.ports (single source of
# truth); re-export here so existing imports keep working while
# consumers migrate to the canonical home.

__all__ = ["CloudPolicyError", "DerivationGateway", "DerivationService"]


class CloudPolicyError(PermissionError):
    pass


class DerivationService:
    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        extraction: EntityExtractionService | None = None,
        event_time: bool | None = None,
    ) -> None:
        self.repository = repository
        # Step 3: find "when it happened" in each derived memory (SEVERIAN_EVENT_TIME; off by default).
        self._event_time = event_time_enabled() if event_time is None else event_time
        # Optional deterministic graph extractor (P1c). When provided,
        # every derive pass also upserts entities/relations so the
        # graph store stays populated without manual seeding.
        self._extraction = extraction

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
                    scope=scope,
                    kind=kind,
                    content=content,
                    source_observation_ids=[observation.id],
                    model_id=model_id,
                    prompt_version=prompt_version,
                    confidence=float(candidate.get("confidence", 0.5)),
                )
            )
        return self.repository.publish_derivation(
            scope,
            entities=extracted_entities,
            relations=extracted_relations,
            memories=validated,
            source_observation_id=observation.id,
        )
