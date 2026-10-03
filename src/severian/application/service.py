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

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from severian.application.derivation import DerivationGateway, DerivationService
from severian.application.ingestion import IngestionResult, IngestionService
from severian.application.ports import CanonicalRepository, LexicalIndex
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalResult, RetrievalService
from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import ERASED, Memory, Observation, Scope


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

    @classmethod
    def from_result(cls, result: RetrievalResult) -> SearchHitView:
        return cls(
            memory_id=result.memory.id,
            content=result.memory.content,
            source_observation_ids=list(result.source_observation_ids),
            lexical_score=result.lexical_score,
            vector_score=result.vector_score,
            fused_score=result.fused_score,
            scope=result.memory.scope,
        )


@dataclass(frozen=True)
class HealthReport:
    ready: bool
    components: dict[str, str] = field(default_factory=dict)


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
    ) -> None:
        self._repository = repository
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
        result = self._ingestion.ingest(scope, content, dict(source or {}))
        return ObservationView.from_result(result)

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
        revision = self._repository.revise_observation(scope, observation_id, content, derive=True)
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
    ) -> list[SearchHitView]:
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
        results = self._retrieval.search(
            scope,
            query,
            limit=limit,
            min_vector_score=min_vector_score,
            min_fused_score=min_fused_score,
            min_coverage=min_coverage,
            salience_weight=salience_weight,
            salience_halflife_h=salience_halflife_h,
        )
        # Tier 2 usage tracking: record a hit for each returned memory so
        # curation salience reflects real retrieval usage. Opt-in (a
        # benchmark read must not mutate state by default).
        if track_usage:
            for item in results:
                self._repository.record_memory_usage(scope, item.memory.id)
        return [SearchHitView.from_result(item) for item in results]

    def assemble_context(
        self,
        scope: Scope,
        hits: list[SearchHitView],
        *,
        max_chars: int | None = None,
    ) -> str:
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
        return assembler.assemble(results)

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
    "SearchHitView",
]
