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
from severian.application.ports import CanonicalRepository
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalResult, RetrievalService
from severian.domain.errors import NotFoundError
from severian.domain.models import Memory, Observation, Scope
from severian.infrastructure.fts import FTS5Index


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
        lexical: FTS5Index | None = None,
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
        # Tombstone the old projection head; index the replacement.
        self._projection.remove_memory(memory_id)
        self._projection.index_memory(revision)
        return revision

    def revise_observation(
        self,
        scope: Scope,
        observation_id: str,
        content: str,
    ) -> Observation:
        """Revise a source observation and propagate the correction (P0-5).

        Memories derived from the OLD head are superseded (dropped from
        default reads and projections); the revised head is re-derived
        through the same canonical pipeline on the next derive pass.
        """
        revision = self._repository.revise_observation(scope, observation_id, content)
        superseded = self._repository.supersede_memories_for_source(scope, observation_id)
        if superseded:
            # The worker's per-scope rebuild will drop the tombstones;
            # the revise path also converges now so search cannot return
            # a stale fact before the next drain.
            self._projection.rebuild(scope)
        return revision

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
        salience_weight: float = 0.0,
        salience_halflife_h: float = 720.0,
        track_usage: bool = False,
    ) -> list[SearchHitView]:
        if derive and gateway is None:
            raise ValueError("derive=True requires a derivation gateway")
        if derive:
            # Ensure observations are derived into memories, then reindex
            # before retrieval. The service is the only place that owns
            # the ordering: derive -> project -> retrieve.
            for observation in self._repository.list_observations(scope):
                if observation.superseded_by_id is not None:
                    continue
                self._derivation.derive_observation(
                    scope, observation.id, gateway, policy=dict(policy or {})  # type: ignore[arg-type]
                )
            self._projection.rebuild(scope)
        results = self._retrieval.search(
            scope,
            query,
            limit=limit,
            min_vector_score=min_vector_score,
            min_fused_score=min_fused_score,
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
        # Re-derive RetrievalResult shape from view to avoid coupling
        # adapters to internal dataclasses.
        proxies = [
            RetrievalResult(
                memory=_MemoryProxy(  # type: ignore[arg-type]
                    id=hit.memory_id,
                    scope=hit.scope,
                    content=hit.content,
                    source_observation_ids=hit.source_observation_ids,
                ),
                source_observation_ids=hit.source_observation_ids,
                lexical_score=hit.lexical_score,
                vector_score=hit.vector_score,
                fused_score=hit.fused_score,
            )
            for hit in hits
        ]
        return assembler.assemble(proxies)

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


@dataclass
class _MemoryProxy:
    """Minimal Memory-shaped proxy used only for context assembly."""

    id: str
    scope: Scope
    content: str
    source_observation_ids: list[str]




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
