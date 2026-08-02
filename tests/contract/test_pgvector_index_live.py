"""Live PostgreSQL + pgvector integration tests.

Exercises the full Postgres stack — canonical backend, pgvector
projection, derivation worker, and retrieval — the same way the
SQLite integration suite does, but against a live server.

Skipped unless ``SEVERIAN_PG_DSN`` is set (see
``docs/postgresql-backend.md``).
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import RetrievalService
from severian.application.worker import DerivationWorker
from severian.domain.models import Memory, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.pgvector_index import PGVectorIndex
from severian.infrastructure.postgresql import PostgreSQLBackend

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")

pytestmark = pytest.mark.skipif(
    not PG_DSN,
    reason="SEVERIAN_PG_DSN not set; skipping live PostgreSQL integration",
)


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


@pytest.fixture
def backend() -> Any:
    repo = PostgreSQLBackend(dsn=PG_DSN)
    os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
    repo.reset(acknowledge=True)
    return repo


def _scope(**overrides: str) -> Scope:
    base = {
        "tenant_id": f"t-{uuid.uuid4().hex[:8]}",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }
    base.update(overrides)
    return Scope(**base)


def test_full_postgres_stack_ingest_derive_retrieve(tmp_path) -> None:
    """Canonical Postgres + pgvector + worker: observations promote to
    memories and retrieval finds them via the vector projection."""
    repo = PostgreSQLBackend(dsn=PG_DSN)
    repo.reset(acknowledge=True)
    repo.initialize()

    vector_index = PGVectorIndex(dsn=PG_DSN)
    vector_index.initialize()
    fts = FTS5Index(tmp_path / "fts.db")

    try:
        scope = _scope()
        IngestionService(repo).ingest(
            scope, "Severian is a local-first memory platform", {}
        )
        IngestionService(repo).ingest(
            scope, "The memory engine derives facts from observations", {}
        )

        derivation = DerivationService(repo)
        coordinator = ProjectionCoordinator(repo, fts, vector_index)
        worker = DerivationWorker(repo, derivation, coordinator)
        summary = worker.drain(
            worker_id="pg-test-worker", gateway=FakeGateway(), policy={}, max_jobs=10
        )
        assert summary.processed >= 2, "worker should have derived at least two memories"

        memories = repo.list_memories(scope)
        assert len(memories) >= 2
        for memory in memories:
            vector_index.upsert(memory)

        assert vector_index.count(scope) == len(memories)

        hits = vector_index.search(scope, "memory platform", limit=5)
        assert hits, "pgvector search should return hits"
        assert any(memory.id == hits[0].memory_id for memory in memories)

        other = scope.model_copy(update={"profile_id": "p2"})
        assert vector_index.count(other) == 0
        assert vector_index.search(other, "memory platform", limit=5) == []

        # blank scope rejected before touching storage
        blanked = scope.model_copy(update={"tenant_id": " "})
        with pytest.raises(ValueError):
            vector_index.search(blanked, "memory platform")
    finally:
        fts.close()
        vector_index.close()
        repo.close()


def test_retrieval_service_uses_pgvector_index(tmp_path) -> None:
    """RetrievalService works end-to-end against the Postgres stack."""
    repo = PostgreSQLBackend(dsn=PG_DSN)
    repo.reset(acknowledge=True)
    repo.initialize()
    vector_index = PGVectorIndex(dsn=PG_DSN)
    vector_index.initialize()
    fts = FTS5Index(tmp_path / "fts.db")

    try:
        scope = _scope()
        observation = IngestionService(repo).ingest(
            scope, "Ada Lovelace wrote the first algorithm", {}
        ).observation
        memory = repo.add_memory(
            Memory(
                scope=scope,
                kind="fact",
                content="Ada Lovelace is the founder of computing",
                source_observation_ids=[observation.id],
                model_id="local/model",
                prompt_version="prompt-v1",
                confidence=0.9,
            )
        )
        vector_index.upsert(memory)

        retrieval = RetrievalService(repo, fts, vector_index)
        results = retrieval.search(scope, "Ada Lovelace computing", limit=5)
        assert results, "retrieval should return the indexed memory"
        assert results[0].memory.id == memory.id
    finally:
        fts.close()
        vector_index.close()
        repo.close()


def test_pgvector_scope_isolation_and_idempotent_upsert() -> None:
    repo = PostgreSQLBackend(dsn=PG_DSN)
    repo.reset(acknowledge=True)
    repo.initialize()
    vector_index = PGVectorIndex(dsn=PG_DSN)
    vector_index.initialize()

    try:
        scope = _scope()
        memory = Memory(
            scope=scope,
            kind="fact",
            content="isolated scope memory",
            source_observation_ids=["obs-1"],
            model_id="local/model",
            prompt_version="prompt-v1",
            confidence=0.9,
        )
        vector_index.upsert(memory)
        vector_index.upsert(memory)  # idempotent

        assert vector_index.count(scope) == 1
        assert vector_index.search(scope, "isolated", limit=5)[0].memory_id == memory.id

        other_scope = _scope(session_id="other-session")
        assert vector_index.count(other_scope) == 0

        vector_index.delete(memory.id)
        assert vector_index.count(scope) == 0
    finally:
        vector_index.close()
        repo.close()
