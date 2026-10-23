"""Canonical application service.

``MemoryService`` is the only entry point for adapters (API, CLI, SDK,
future MCP/Hermes) to perform mutating and read operations. It composes
the Phase 1 repository with the Phase 2 ingestion, derivation, retrieval
and projection services and returns plain value objects.

Adapters are explicitly forbidden from writing the repository or
projection indexes directly. ``BuildMemoryService`` is the dependency
injection seam for tests and runtime composition; the same builder
backed by on-disk SQLite is the production wiring.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from severian.application.anticipation import MAX_REMINDERS, DueContext, anticipation_enabled, match_due
from severian.application.conflicts import conflict_note, conflict_notes_enabled
from severian.application.derivation import DerivationGateway, DerivationService
from severian.application.emptyitems import demote_empty, drop_empty_enabled
from severian.application.eventtime import as_utc, event_time_enabled, query_window
from severian.application.factslots import asks_about_past
from severian.application.fillbudget import MIN_FILL_ROOM, fill_budget_enabled, neighbour_memories
from severian.application.ingestion import IngestionResult, IngestionService
from severian.application.ports import CanonicalRepository, LexicalIndex, TaskNoteRepository
from severian.application.projections import ProjectionCoordinator
from severian.application.replytrim import reply_trim_enabled
from severian.application.rerank import Reranker, rerank_pool, reranked
from severian.application.retrieval import ContextAssembler, RetrievalResult, RetrievalService
from severian.application.statements import assess_truth, drop_replaced
from severian.application.truthresolution import truth_resolution_enabled
from severian.application.userfacts import FACT_KIND, FACTS_JOB, user_facts_enabled
from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import ERASED, Memory, Observation, Scope, TaskNote, TruthState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FactsRollback:
    """What ``rollback_user_facts`` did: scopes that had facts, facts erased, fact jobs cancelled."""

    scopes: int
    erased: int
    cancelled_jobs: int


@dataclass(frozen=True)
class ObservationView:
    """Stable transport-friendly view of an ingestion outcome."""

    observation_id: str
    content: str
    content_hash: str
    scope: Scope
    source: Mapping[str, Any]
    job_id: str
    job_kind: str

    @classmethod
    def from_result(cls, result: IngestionResult) -> ObservationView:
        return cls(
            observation_id=result.observation.id,
            content=result.observation.content,
            content_hash=result.observation.content_hash,
            scope=result.observation.scope,
            source=dict(result.observation.source),
            job_id=result.job.id,
            job_kind=result.job.kind,
        )


@dataclass(frozen=True)
class SearchHitView:
    memory_id: str
    content: str
    source_observation_ids: list[str]
    lexical_score: float
    vector_score: float
    fused_score: float
    scope: Scope
    # Step 1 truth state; ``current`` unless the truth-state view labelled it otherwise.
    truth_state: str = TruthState.CURRENT.value
    replaced_by_id: str | None = None
    # Contents this hit replaced (the "changes" view).
    previous: tuple[str, ...] = ()
    # Event time (Step 3): the period the memory says the thing happened in, when known.
    occurred_at: datetime | None = None
    occurred_until: datetime | None = None
    # Truth resolution (SEVERIAN_TRUTH_RESOLUTION): when a newer fact replaced this one; None while current.
    valid_until: datetime | None = None

    @classmethod
    def from_result(
        cls,
        result: RetrievalResult,
        *,
        truth_state: TruthState = TruthState.CURRENT,
        replaced_by_id: str | None = None,
        previous: tuple[str, ...] = (),
        valid_until: datetime | None = None,
    ) -> SearchHitView:
        return cls(
            memory_id=result.memory.id,
            content=result.memory.content,
            source_observation_ids=list(result.source_observation_ids),
            lexical_score=result.lexical_score,
            vector_score=result.vector_score,
            fused_score=result.fused_score,
            scope=result.memory.scope,
            truth_state=truth_state.value,
            replaced_by_id=replaced_by_id,
            previous=previous,
            occurred_at=result.memory.occurred_at,
            occurred_until=result.memory.occurred_until,
            valid_until=valid_until,
        )


SEARCH_VIEWS = ("now", "history", "changes")


def _as_of_moment(as_of: datetime | date | None) -> datetime | None:
    """A UTC moment for ``as_of``: a date means the end of that day, a datetime is used as given."""
    if as_of is None:
        return None
    if isinstance(as_of, datetime):
        return as_utc(as_of)
    return datetime(as_of.year, as_of.month, as_of.day, tzinfo=UTC) + timedelta(days=1) - timedelta(microseconds=1)


def truth_state_enabled() -> bool:
    """``SEVERIAN_TRUTH_STATE`` switches the Step 1 truth-state rules on (off = the legacy rule)."""
    return os.environ.get("SEVERIAN_TRUTH_STATE", "").strip().lower() in {"1", "true", "on", "yes"}


@dataclass(frozen=True)
class HealthReport:
    ready: bool
    components: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DueView:
    """An instruction-for-later whose trigger fits the current situation (Step 4)."""

    memory_id: str
    content: str
    source_observation_ids: tuple[str, ...]


class MemoryService:
    """The only sanctioned write/read path for adapters."""

    def __init__(
        self,
        repository: CanonicalRepository,
        ingestion: IngestionService,
        retrieval: RetrievalService,
        projection: ProjectionCoordinator,
        derivation: DerivationService,
        context: ContextAssembler,
        *,
        lexical: LexicalIndex | None = None,
        truth_state: bool | None = None,
        event_time: bool | None = None,
        anticipation: bool | None = None,
        user_facts: bool | None = None,
        truth_resolution: bool | None = None,
        reply_trim: bool | None = None,
        conflict_notes: bool | None = None,
        drop_empty: bool | None = None,
        fill_budget: bool | None = None,
    ) -> None:
        # Round 3 fixes, each off by default (None = read its SEVERIAN_* switch):
        # (a) SEVERIAN_REPLY_TRIM: when a context does not fit, the assistant's replies go before any user words.
        # (b) SEVERIAN_CONFLICT_NOTES: a note naming two returned memories that disagree.
        # (c) SEVERIAN_DROP_EMPTY: results that carry nothing move behind all the others.
        # (d) SEVERIAN_TIE_ORDER was removed after the 21/10/2026 re-test: no change in answers.
        self._reply_trim = reply_trim_enabled() if reply_trim is None else reply_trim
        self._conflict_notes = conflict_notes_enabled() if conflict_notes is None else conflict_notes
        self._drop_empty = drop_empty_enabled() if drop_empty is None else drop_empty
        # Handoff 1 (SEVERIAN_FILL_BUDGET; off by default): assembly spends the character budget the caller's own
        # items leave empty on the turns around them, appended after those items, with no extra search.
        self._fill_budget = fill_budget_enabled() if fill_budget is None else fill_budget
        # Handoff 1 (SEVERIAN_TRUTH_RESOLUTION; off by default): memories a newer fact closed leave the "now"
        # view and show up, dated, in "history"; a question about the past reads "history".
        self._truth_resolution = truth_resolution_enabled() if truth_resolution is None else truth_resolution
        self._anticipation = anticipation_enabled() if anticipation is None else anticipation
        self._truth_state = truth_state_enabled() if truth_state is None else truth_state
        self._event_time = event_time_enabled() if event_time is None else event_time
        # Round 2 idea 1 (SEVERIAN_USER_FACTS): every saved turn also queues a background fact job.
        self._user_facts = user_facts_enabled() if user_facts is None else user_facts
        self._repository = repository
        # Round 2 improvement 4 (SEVERIAN_RERANK): optional cross-encoder; None = search exactly as before.
        self._reranker: Reranker | None = None
        self._rerank_pool = rerank_pool()
        self._ingestion = ingestion
        self._retrieval = retrieval
        self._projection = projection
        self._derivation = derivation
        self._context = context
        self._lexical = lexical

    # -- writes ---------------------------------------------------------

    def ingest_observation(
        self,
        scope: Scope,
        *,
        content: str,
        source: Mapping[str, Any] | None = None,
    ) -> ObservationView:
        result = self._ingestion.ingest(scope, content, dict(source or {}), also_queue=self._fact_jobs())
        return ObservationView.from_result(result)

    def _fact_jobs(self) -> tuple[str, ...]:
        """Extra job kinds for a saved turn: the background fact job while SEVERIAN_USER_FACTS is on.

        Queued in the same transaction as the turn (and as a correction's revision), so a turn can
        never be saved without its fact job; if that write fails, the save fails as a whole.
        """
        return (FACTS_JOB,) if self._user_facts else ()

    def correct_memory(
        self,
        scope: Scope,
        memory_id: str,
        *,
        content: str,
        model_id: str,
        prompt_version: str,
    ) -> Memory:
        """Correct a derived memory: revise, tombstone the old head from
        projections, and index the new revision (P0-5).

        The repository records the revision chain; the projection is
        updated so the stale fact never resurfaces in search.
        """
        revision = self._repository.revise_memory(
            scope,
            memory_id,
            content=content,
            model_id=model_id,
            prompt_version=prompt_version,
        )
        # DATA-03: the canonical revision is durable the moment revise_memory
        # commits. The projection update is best-effort targeted, then
        # reconciled: if the targeted tombstone/index fails for any reason,
        # fall back to a full digest-reconciling rebuild of the scope so the
        # correction is end-to-end durable (search never keeps surfacing the
        # stale head).
        try:
            # Tombstone the old projection head; index the replacement.
            self._projection.remove_memory(memory_id)
            self._projection.index_memory(revision)
        except Exception:
            # Targeted update failed — rebuild the whole scope's projections
            # from canonical so they converge with the committed revision.
            self._projection.rebuild(scope)
        return revision

    def revise_observation(
        self,
        scope: Scope,
        observation_id: str,
        content: str,
    ) -> Observation:
        """Revise a source observation and propagate the correction (P0-5).

        Memories derived from the OLD head are superseded (dropped from
        default reads and projections) and graph facts lose that source
        (retracted when it was their last). The revision's derive job is
        queued in the same transaction as the revision, so the corrected
        fact replaces the old one on the next drain instead of vanishing.
        """
        revision = self._repository.revise_observation(
            scope, observation_id, content, derive=True, also_queue=self._fact_jobs()
        )
        superseded = self._repository.supersede_memories_for_source(scope, observation_id)
        self._repository.retract_relations_for_source(scope, observation_id)
        if superseded:
            # The worker's per-scope rebuild will drop the tombstones;
            # the revise path also converges now so search cannot return
            # a stale fact before the next drain.
            self._projection.rebuild(scope)
        return revision

    # -- erasure lifecycle (OPS-02) -------------------------------------

    def erase_memory(
        self,
        scope: Scope,
        memory_id: str,
        *,
        reason: str = "erased",
    ) -> Memory:
        """Erase a memory from active reads and search (OPS-02).

        Deletion is tombstone-based, never a hard delete: the memory is
        superseded by a synthetic ``:erased`` tombstone so default reads
        and projections stop returning it, while the immutable history
        retains the record (and the tombstone) for audit. Re-derivation
        of the same fact via a fresh observation produces a NEW memory —
        erasure does not poison future ingestion.
        """
        original = self._repository.get_memory(scope, memory_id)
        if original.superseded_by_id is not None:
            raise ConflictError("memory is already superseded")
        # Tombstone record: a revision of the original whose OWN
        # superseded_by_id is the reserved sentinel ``:erased`` — so the
        # tombstone is excluded from active reads (superseded_by_id is
        # non-NULL) while remaining in history for audit. The original is
        # then superseded BY the tombstone, closing the chain.
        tombstone = Memory(
            scope=scope,
            kind=original.kind,
            content=f"[{reason}]",
            source_observation_ids=original.source_observation_ids,
            model_id=original.model_id,
            prompt_version=f"{original.prompt_version}:erased",
            confidence=0.0,
            revision_of_id=original.id,
            superseded_by_id=ERASED,
        )
        inserted = self._repository.add_memory(tombstone)
        erased = self._repository.supersede_memory(scope, memory_id, inserted.id)
        try:
            self._projection.remove_memory(memory_id)
        except Exception:
            self._projection.rebuild(scope)
        return erased

    def rollback_user_facts(self, tenant_id: str, profile_id: str) -> FactsRollback:
        """Undo SEVERIAN_USER_FACTS for one profile: erase every fact it made, keep everything else.

        Only ``user_fact`` memories exist because of the switch, so only they are erased, in every
        scope (collection, agent, session) of the profile, through the normal erasure path
        (tombstoned: gone from reads and search, kept in history for audit). Raw turns, memories
        from the normal derive pass and other profiles are untouched. Pending fact jobs are
        cancelled first so a later worker run cannot re-create the facts. Refuses while the switch
        is on in this process's environment (writers would queue fresh jobs) or while a fact job
        is running. Safe to repeat.
        """
        if user_facts_enabled():
            raise RuntimeError("turn SEVERIAN_USER_FACTS off (and restart writers) before rolling back")
        scopes = self._repository.list_profile_scopes(tenant_id, profile_id)
        cancelled = sum(self._repository.cancel_pending_jobs(scope, FACTS_JOB) for scope in scopes)
        erased = touched = 0
        for scope in scopes:
            made = [m for m in self._repository.list_memories(scope) if m.kind == FACT_KIND]
            for memory in made:
                self.erase_memory(scope, memory.id, reason="user facts rolled back")
            erased += len(made)
            touched += bool(made)
        return FactsRollback(scopes=touched, erased=erased, cancelled_jobs=cancelled)

    def erase_observation(
        self,
        scope: Scope,
        observation_id: str,
        *,
        reason: str = "erased",
    ) -> Observation:
        """Erase a source observation and everything derived from it (OPS-02).

        The observation is superseded by an erasure tombstone that is itself
        never active (so it is never pending derivation and never surfaces),
        every active memory derived from it is erased via the same tombstone
        path, and graph facts lose it as a source (retracted when it was
        their last). Search stops returning facts whose only source was
        erased; history keeps the records for audit.
        """
        original = self._repository.get_observation(scope, observation_id)
        if original.superseded_by_id is not None:
            raise ConflictError("observation is already superseded")
        self._repository.revise_observation(scope, observation_id, f"[{reason}]", tombstone=True)
        for memory in self._repository.list_memories(scope):
            if (
                memory.superseded_by_id is None
                and observation_id in memory.source_observation_ids
            ):
                self.erase_memory(scope, memory.id, reason=reason)
        self._repository.retract_relations_for_source(scope, observation_id)
        self._projection.rebuild(scope)
        return self._repository.get_observation(scope, observation_id)

    # -- reads ----------------------------------------------------------

    # -- task notes (never derived, projected or searched) --------------

    def _task_notes(self) -> TaskNoteRepository:
        if not isinstance(self._repository, TaskNoteRepository):
            raise NotImplementedError("this storage backend does not support task notes")
        return self._repository

    def set_task_note(self, scope: Scope, task: str, state: str, metadata: Mapping[str, Any] | None = None) -> TaskNote:
        return self._task_notes().set_task_note(scope, task, state, dict(metadata or {}))

    def get_task_note(self, scope: Scope, task: str) -> tuple[TaskNote | None, list[TaskNote]]:
        return self._task_notes().get_task_note(scope, task)

    def list_task_notes(self, scope: Scope) -> list[TaskNote]:
        return self._task_notes().list_task_notes(scope)

    def clear_task_note(self, scope: Scope, task: str) -> bool:
        return self._task_notes().clear_task_note(scope, task)

    def get_observation(self, scope: Scope, observation_id: str) -> Observation:
        # ``CanonicalRepository.get_observation`` already filters by scope
        # in SQLite. We forward unchanged; cross-scope access raises
        # ``NotFoundError`` from the storage layer.
        return self._repository.get_observation(scope, observation_id)

    def get_memory(self, scope: Scope, memory_id: str) -> Memory:
        """Resolve a projected memory by id within scope.

        P1-4: search results are memory ids (both observation-derived
        and derived memories). The API needs a memory-detail endpoint so
        the dashboard can open any search hit without assuming it is an
        observation. Cross-scope access raises ``NotFoundError``.
        """
        return self._repository.get_memory(scope, memory_id)

    def rollback_memory(self, scope: Scope, revision_id: str) -> Memory:
        """Restore a historical revision as the active memory head (A-2).

        ChronoMem-style rollback: reverses a bad correction. The
        restored revision becomes the active head; the current head is
        superseded. The projection is rebuilt so retrieval reflects the
        rollback immediately.
        """
        restored = self._repository.rollback_memory(scope, revision_id)
        self._projection.rebuild(scope)
        return restored

    def derive_pending(
        self,
        scope: Scope,
        gateway: DerivationGateway,
        *,
        max_observations: int = 50,
        policy: Mapping[str, Any] | None = None,
    ) -> int:
        """Derive pending observations into memories WITHOUT retrieving.

        The benchmark warmup path (zero-debt P0.3): a scale lane must
        warm the index to the bucket size BEFORE timing hot queries, but
        ``search(derive=True)`` re-runs the full retrieval + projection
        pass per call — super-linear across 10k/100k. This method loops
        ONLY the derive step (bounded per call by ``max_observations``,
        same OPS-03 cap semantics), projects the outbox delta once, and
        returns the number of fresh memories derived. Retrieval is
        never executed; counting uses the cheap repository COUNT.
        """
        if gateway is None:
            raise ValueError("derive_pending requires a derivation gateway")
        pending = self._repository.list_pending_observations(
            scope, limit=max_observations
        )
        derived = 0
        for observation in pending:
            produced = self._derivation.derive_observation(
                scope, observation.id, gateway, policy=dict(policy or {})
            )
            if produced:
                derived += 1
                if derived >= max_observations:
                    break
        if derived:
            pending_events = [
                e for e in self._repository.pending_outbox()
                if e.scope == scope
            ]
            if pending_events:
                self._projection.apply(pending_events)
                try:
                    self._repository.ack_outbox_many([e.id for e in pending_events])
                except Exception:  # noqa: BLE001 - idempotent; next pass retries
                    pass
        return derived

    def due(
        self,
        scope: Scope,
        *,
        tool: str = "",
        path: str = "",
        repo: str = "",
        message: str = "",
        limit: int = MAX_REMINDERS,
    ) -> list[DueView]:
        """Instructions-for-later whose trigger matches the current tool, path, repo or message.

        At most two (``MAX_REMINDERS``), oldest first. Empty when anticipation is switched off or
        nothing fits; reads only the memories that carry a trigger, never the whole store.
        """
        if not self._anticipation:
            return []
        context = DueContext(tool=tool, path=path, repo=repo, message=message)
        candidates = self._repository.list_trigger_memories(scope)
        return [
            DueView(memory.id, memory.content, tuple(memory.source_observation_ids))
            for memory in match_due(candidates, context, limit=limit)
        ]

    def search(
        self,
        scope: Scope,
        *,
        query: str,
        limit: int = 20,
        derive: bool = False,
        gateway: DerivationGateway | None = None,
        policy: Mapping[str, Any] | None = None,
        min_vector_score: float | None = None,
        min_fused_score: float | None = None,
        min_coverage: float | None = None,
        salience_weight: float = 0.0,
        salience_halflife_h: float = 720.0,
        track_usage: bool = False,
        derive_max_observations: int = 50,
        view: str = "now",
        as_of: datetime | date | None = None,
    ) -> list[SearchHitView]:
        """Search. ``as_of`` (a date or datetime) answers as the store stood then: only memories whose
        source message was written by that moment, so a later correction does not yet exist."""
        if view not in SEARCH_VIEWS:
            raise ValueError(f"view must be one of {', '.join(SEARCH_VIEWS)}")
        if derive and gateway is None:
            raise ValueError("derive=True requires a derivation gateway")
        if derive:
            # Ensure observations are derived into memories, then reindex
            # before retrieval. The service is the only place that owns
            # the ordering: derive -> project -> retrieve.
            # OPS-03: synchronous derive is BOUNDED — at most
            # ``derive_max_observations`` NEW memories are derived per
            # call (oldest first), so a caller cannot accidentally
            # trigger an unbounded O(n) synchronous derivation + rebuild
            # on a large scope. Already-derived observations (idempotent
            # dedupe) do NOT count against the bound — only fresh work
            # does. Anything beyond the bound must go through the durable
            # worker (enqueue + drain).
            #
            # zero-debt 3.1: pending work is answered by a bounded SQL
            # query, NOT by scanning every observation and counting the
            # whole scope per item. On a fully derived scope this is a
            # single indexed lookup that derives zero and stays close to
            # derive=False; each returned observation is fresh by
            # construction (no memory references it yet).
            pending = self._repository.list_pending_observations(
                scope, limit=derive_max_observations
            )
            derived = 0
            for observation in pending:
                produced = self._derivation.derive_observation(
                    scope, observation.id, gateway, policy=dict(policy or {})  # type: ignore[arg-type]
                )
                # Only observations that produced memories count against
                # the bound (OPS-03: fresh work only). A revision
                # observation whose content is already covered (C-08)
                # derives zero and does NOT consume a slot — preserving
                # the pre-3.1 semantics without a full-scope count.
                if produced:
                    derived += 1
                    if derived >= derive_max_observations:
                        break
            if derived:
                # zero-debt 3.2: project the outbox delta incrementally —
                # the memories just created appended their events; apply
                # ONLY those (bounded upsert/delete), never a scope-wide
                # clear+reinsert. rebuild() remains the repair/admin path.
                pending_events = [
                    e for e in self._repository.pending_outbox()
                    if e.scope == scope
                ]
                if pending_events:
                    self._projection.apply(pending_events)
        # Improvement 2: a newer statement of the same fact replaces the older
        # one in active results. Over-fetch so a replaced memory never costs
        # the caller a result slot.
        as_of_moment = _as_of_moment(as_of)
        window = None
        if self._event_time and query.strip() not in {"", "*"}:
            window = query_window(query, as_of_moment or datetime.now(UTC))
        pool = self._retrieval.search(
            scope,
            query,
            as_of=as_of_moment,
            window=window,
            limit=max(min(limit * 3, limit + 50), self._rerank_pool if self._reranker else 0),
            min_vector_score=min_vector_score,
            min_fused_score=min_fused_score,
            min_coverage=min_coverage,
            salience_weight=salience_weight,
            salience_halflife_h=salience_halflife_h,
        )
        if self._reranker is not None and query.strip() not in {"", "*"}:
            pool = reranked(
                query, pool, self._reranker, text=lambda item: item.memory.content, pool=self._rerank_pool
            )
        if self._drop_empty and query.strip() not in {"", "*"}:
            # Fix (c): results that carry nothing give their place to the next one (kept last, never deleted).
            pool = demote_empty(pool, content=lambda item: item.memory.content, query=query)
        closed_labels: dict[str, dict[str, Any]] = {}
        if self._truth_resolution:
            if view == "now" and asks_about_past(query):
                view = "history"
            pool, closed_labels = self._apply_closures(scope, pool, view=view, as_of=as_of_moment)
        if self._truth_state or view != "now":
            results, labels = self._truth_view(pool, view=view, limit=limit)
        else:
            results = drop_replaced(
                pool,
                content=lambda item: item.memory.content,
                created_at=lambda item: item.memory.created_at,
                key=lambda item: item.memory.id,
            )[:limit]
            labels = {}
        # Tier 2 usage tracking: record a hit for each returned memory so
        # curation salience reflects real retrieval usage. Opt-in (a
        # benchmark read must not mutate state by default).
        if track_usage:
            for item in results:
                self._repository.record_memory_usage(scope, item.memory.id)
        for memory_id, extra in closed_labels.items():
            labels[memory_id] = {**labels.get(memory_id, {}), **extra}
        if self._truth_resolution and view == "changes":
            # What a newer fact closed counts as a change even when the words are too different for the
            # text rule; keep the pool's order and the caller's limit.
            replacers = {mid for mid, extra in closed_labels.items() if extra.get("previous")}
            already = {item.memory.id for item in results}
            results = [item for item in pool if item.memory.id in replacers or item.memory.id in already][:limit]
        views = [SearchHitView.from_result(item, **labels.get(item.memory.id, {})) for item in results]
        return views

    def conflict_note(self, hits: Sequence[SearchHitView], *, query: str | None = None) -> str | None:
        """Fix (b): a note naming up to two disagreements among ``hits``; None if none or the switch is off."""
        if not self._conflict_notes:
            return None
        return conflict_note([hit.content for hit in hits], query=query)

    def _apply_closures(
        self, scope: Scope, pool: list[RetrievalResult], *, view: str, as_of: datetime | None
    ) -> tuple[list[RetrievalResult], dict[str, dict[str, Any]]]:
        """Memories a newer fact closed: out of ``now``; in ``history``/``changes``, dated and linked.

        As of a date before the close, the memory was still current, so it is not treated as closed.
        """

        def closed(item: RetrievalResult) -> bool:
            until = item.memory.valid_until
            return until is not None and (as_of is None or as_utc(until) <= as_of)

        if view == "now":
            return [item for item in pool if not closed(item)], {}
        # Who replaced whom, including predecessors that did not rank into the pool themselves.
        wanted = {item.memory.id for item in pool}
        predecessors: dict[str, dict[str, Memory]] = {}
        for memory in self._repository.list_memories(scope):
            target = memory.replaced_by_id
            if target is None or target not in wanted or memory.valid_until is None:
                continue
            if as_of is None or as_utc(memory.valid_until) <= as_of:
                predecessors.setdefault(target, {})[memory.id] = memory
        labels: dict[str, dict[str, Any]] = {}
        for item in pool:
            memory = item.memory
            extra: dict[str, Any] = {}
            if closed(item):
                extra = {
                    "truth_state": TruthState.HISTORICAL,
                    "replaced_by_id": memory.replaced_by_id,
                    "valid_until": memory.valid_until,
                }
            if memory.id in predecessors:
                earlier = sorted(predecessors[memory.id].values(), key=lambda m: (m.created_at, m.id))
                extra["previous"] = tuple(m.content for m in earlier)
                if view == "changes":
                    extra["truth_state"] = TruthState.TRANSITION
            if extra:
                labels[memory.id] = extra
        return pool, labels

    @staticmethod
    def _truth_view(
        pool: list[RetrievalResult], *, view: str, limit: int
    ) -> tuple[list[RetrievalResult], dict[str, dict[str, Any]]]:
        """Apply the Step 1 truth state to a ranked pool (``now`` | ``history`` | ``changes``)."""
        assessed = assess_truth(
            pool,
            content=lambda item: item.memory.content,
            created_at=lambda item: item.memory.created_at,
            key=lambda item: item.memory.id,
        )
        contents = {item.memory.id: item.memory.content for item in pool}
        labels = {
            memory_id: {
                "truth_state": truth.state,
                "replaced_by_id": truth.replaced_by,
                "previous": tuple(contents[old] for old in truth.previous),
            }
            for memory_id, truth in assessed.items()
        }
        if view == "history":
            kept = list(pool)
        elif view == "changes":
            kept = [item for item in pool if assessed[item.memory.id].previous]
            for item in kept:
                labels[item.memory.id]["truth_state"] = TruthState.TRANSITION
        else:
            kept = [item for item in pool if assessed[item.memory.id].state is not TruthState.HISTORICAL]
        return kept[:limit], labels

    def assemble_context(
        self,
        scope: Scope,
        hits: list[SearchHitView],
        *,
        max_chars: int | None = None,
        query: str | None = None,
    ) -> str:
        """The cited context block for ``hits``. ``query`` (the question) lets the round 3 fixes tell when the
        assistant's replies are the evidence (a) and whether a disagreement is about the question (b)."""
        if any(hit.scope != scope for hit in hits):
            raise NotFoundError("context hit is outside the requested scope")
        assembler = self._context if max_chars is None else ContextAssembler(max_chars=max_chars)
        # ARCH-04: assemble from the CANONICAL Memory (fetched by id), not
        # from a reconstructed proxy — the proxy duplicated the Memory
        # shape and could drift from the real model. The repository is the
        # single source of truth for content.
        results: list[RetrievalResult] = []
        for hit in hits:
            memory = self._repository.get_memory(scope, hit.memory_id)
            results.append(
                RetrievalResult(
                    memory=memory,
                    source_observation_ids=memory.source_observation_ids,
                    lexical_score=hit.lexical_score,
                    vector_score=hit.vector_score,
                    fused_score=hit.fused_score,
                )
            )
        stored = [result.memory.content for result in results]
        note = conflict_note(stored, query=query) if self._conflict_notes else None
        block = assembler.assemble(results, query=query, trim_replies=self._reply_trim, note=note)
        if not self._fill_budget or not results:
            return block
        # SEVERIAN_FILL_BUDGET: only spend the room the caller's own items leave. When they already fill the budget
        # (LongMemEval's items are long) nothing is read and the block is returned as built.
        room = assembler.max_chars - len(block)
        if room < MIN_FILL_ROOM:
            return block
        extra = neighbour_memories(self._repository, scope, results)
        if not extra:
            return block
        # The caller's items are laid out first, unchanged and in order; these only fill what they left. No search
        # happens here, so nothing that was ranked can move: the off block is a byte prefix of this one.
        results.extend(
            RetrievalResult(
                memory=memory,
                source_observation_ids=memory.source_observation_ids,
                lexical_score=0.0,
                vector_score=0.0,
                fused_score=0.0,
            )
            for memory in extra
        )
        return assembler.assemble(results, query=query, trim_replies=self._reply_trim, note=note)

    def health(self) -> HealthReport:
        components: dict[str, str] = {}
        # Repository health: cheap SQL probe if the concrete repository
        # exposes ``engine`` (SQLiteRepository and PostgreSQLBackend
        # both do). Adapters must not assume this attribute, so we
        # tolerate absence and treat the repository as healthy in that
        # case (a no-op repository used in tests).
        engine = getattr(self._repository, "engine", None)
        if engine is not None:
            try:
                with engine.connect() as connection:
                    connection.exec_driver_sql("SELECT 1")
                components["repository"] = "ok"
            except Exception:  # pragma: no cover - defensive
                components["repository"] = "error"
        else:
            components["repository"] = "ok"
        # Projection health is adapter-neutral: each index probes its own
        # surface (``connection`` for SQLite, ``engine`` for PostgreSQL),
        # so a healthy PG/pgvector index reports ok (P1-2).
        projection_components = self._projection.health()
        components["lexical"] = projection_components.get("lexical", "ok")
        components["vector"] = projection_components.get("vector", "ok")
        return HealthReport(ready=all(v == "ok" for v in components.values()), components=components)




def BuildMemoryService(  # noqa: N802 (factory convention)
    *,
    repository: CanonicalRepository,
    ingestion: IngestionService,
    retrieval: RetrievalService,
    projection: ProjectionCoordinator,
    derivation: DerivationService,
    context: ContextAssembler | None = None,
) -> MemoryService:
    """Compose a :class:`MemoryService` from Phase 1+2 building blocks.

    ``derivation`` is a :class:`DerivationService` so that cloud-deny
    policy is enforced regardless of the underlying gateway. Tests that
    want to use a custom gateway should construct their own
    ``DerivationService`` and pass it here.
    """
    return MemoryService(
        repository=repository,
        ingestion=ingestion,
        retrieval=retrieval,
        projection=projection,
        derivation=derivation,
        context=context or ContextAssembler(),
        lexical=getattr(retrieval, "lexical", None),
    )


__all__ = [
    "BuildMemoryService",
    "CanonicalRepository",
    "HealthReport",
    "MemoryService",
    "NotFoundError",
    "ObservationView",
    "DueView",
    "SearchHitView",
]
