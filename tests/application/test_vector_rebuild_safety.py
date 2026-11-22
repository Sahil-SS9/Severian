"""0.6.4 A: a scope rebuild that fails part-way keeps every vector; forget/erase/revise touch only what they change.

Regression for 20/11/2026: four severian_forget calls each cleared ~28k vectors, then failed on one
9,238-character memory and left the scope empty.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.application.worker import DerivationWorker
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

POISON = "POISON-TEXT"


class FakeGateway:
    model_id = "local/test-model"
    prompt_version = "prompt-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


class PickyEmbedding(HashEmbedding):
    """Hash space, but refuses any text containing POISON once ``armed`` (like the 8000-char rejection)."""

    def __init__(self) -> None:
        super().__init__(dimension=64)
        self.armed = False
        self.calls = 0

    def encode(self, text: str) -> list[float]:
        self.calls += 1
        if self.armed and POISON in text:
            raise ValueError("embedding refused this text")
        return super().encode(text)


SCOPE = Scope(tenant_id="t1", profile_id="p1", collection_id="c1-t1", agent_id="a1", session_id="s1")


@pytest.fixture
def store(tmp_path: Path) -> Any:
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1-t1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts.db")
    embedding = PickyEmbedding()
    vector = LocalVectorIndex(tmp_path / "vectors.db", embedding)
    projection = ProjectionCoordinator(repo, fts, vector)
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=projection,
        derivation=DerivationService(repo),
        context=ContextAssembler(),
    )
    worker = DerivationWorker(repo, DerivationService(repo), projection)
    ns = dataclasses.make_dataclass("Store", ["repo", "fts", "vector", "embedding", "service", "worker", "path"])
    yield ns(repo, fts, vector, embedding, service, worker, tmp_path)
    fts.close()
    vector.close()
    repo.close()


def _seed(store: Any, texts: list[str]) -> list[str]:
    ids = []
    for text in texts:
        ids.append(store.service.ingest_observation(SCOPE, content=text, source={"provider": "test"}).observation_id)
    store.worker.drain(worker_id="seed", gateway=FakeGateway())
    return ids


def _drop_vectors(store: Any, *, keep_poison: bool, extra: int, spare: frozenset[str] = frozenset()) -> None:
    """Remove the poison memory's vector (so a rebuild must encode it) and ``extra`` others (never ``spare``)."""
    memories = sorted(store.repo.list_memories(SCOPE), key=lambda m: m.id)
    doomed = [m.id for m in memories if POISON in m.content and not keep_poison]
    doomed += [m.id for m in memories if POISON not in m.content and m.id not in spare][:extra]
    for memory_id in doomed:
        store.vector.delete(memory_id)


# -- A: failure-safe rebuild + targeted forget ---------------------------------------------------


def test_failed_scope_rebuild_keeps_every_existing_vector(store: Any) -> None:
    _seed(store, [f"memory number {i} about gardening" for i in range(6)] + [f"{POISON} long memory"])
    _drop_vectors(store, keep_poison=False, extra=2)
    before = store.vector.list_memory_ids(SCOPE)
    assert len(before) == 4
    store.embedding.armed = True
    with pytest.raises(ValueError):
        store.vector.rebuild(store.repo, SCOPE)
    assert store.vector.list_memory_ids(SCOPE) == before, "a failed rebuild must not empty the scope"


def test_failed_coordinator_rebuild_keeps_every_existing_vector(store: Any) -> None:
    _seed(store, [f"note {i} on the boat trip" for i in range(5)] + [f"{POISON} attachment"])
    _drop_vectors(store, keep_poison=False, extra=1)
    before = store.vector.list_memory_ids(SCOPE)
    assert len(before) == 4
    store.embedding.armed = True
    with pytest.raises(ValueError):
        store.service._projection.rebuild(SCOPE)
    assert store.vector.list_memory_ids(SCOPE) == before


def test_forget_touches_only_the_forgotten_memory(store: Any) -> None:
    """severian_forget -> erase_observation: no scope-wide re-encode, other vectors survive."""
    observation_ids = _seed(store, [f"fact {i} about the allotment" for i in range(8)] + [f"{POISON} essay"])
    forgotten = {m.id for m in store.repo.list_memories_for_observations(SCOPE, [observation_ids[0]])}
    # Live state on 22/11: most of the scope had already lost its vectors. A forget must neither
    # re-encode them inside the bot (17-22 min) nor touch any row but the forgotten one.
    _drop_vectors(store, keep_poison=False, extra=3, spare=frozenset(forgotten))
    before = store.vector.list_memory_ids(SCOPE)
    assert len(before) == 5 and forgotten <= before
    store.embedding.armed = True
    store.embedding.calls = 0
    store.service.erase_observation(SCOPE, observation_ids[0], reason="forget")
    after = store.vector.list_memory_ids(SCOPE)
    assert after == before - forgotten, "only the forgotten memory leaves the vector index"
    assert store.embedding.calls == 0, "forget must not re-encode the scope"


def test_rollback_and_revise_observation_converge_without_full_rebuild(store: Any) -> None:
    observation_ids = _seed(store, [f"plan {i} for the weekend" for i in range(4)] + [f"{POISON} list"])
    store.embedding.armed = True
    store.service.revise_observation(SCOPE, observation_ids[1], "plan 1 for the weekend moved to Sunday")
    active = {m.id for m in store.repo.list_memories(SCOPE)}
    assert store.vector.list_memory_ids(SCOPE) <= active, "the superseded memory leaves the index"
    assert len(store.vector.list_memory_ids(SCOPE)) == len(active)


@pytest.mark.skipif(not __import__("os").environ.get("SEVERIAN_PG_DSN"), reason="SEVERIAN_PG_DSN not set")
def test_pgvector_failed_rebuild_keeps_every_existing_vector() -> None:
    import os
    import uuid

    from severian.domain.models import Memory
    from severian.infrastructure.pgvector_index import PGVectorIndex

    table = f"mv_v064_{uuid.uuid4().hex[:10]}"
    embedding = PickyEmbedding()
    index = PGVectorIndex(os.environ["SEVERIAN_PG_DSN"], embedding, table_name=table)
    memories = [
        Memory(
            scope=SCOPE, kind="fact", content=text, source_observation_ids=["o1"],
            model_id="m", prompt_version="v", confidence=1.0,
        )
        for text in [f"pg memory {i}" for i in range(5)] + [f"{POISON} pg memory"]
    ]

    class _Repo:
        def list_memories(self, scope: Scope) -> list[Memory]:
            return list(memories)

    try:
        index.initialize()
        index.upsert_many(memories[:4])  # the poison memory and one other have no vector yet
        before = index.list_memory_ids(SCOPE)
        embedding.armed = True
        with pytest.raises(ValueError):
            index.rebuild(_Repo(), SCOPE)  # type: ignore[arg-type]
        assert index.list_memory_ids(SCOPE) == before
        embedding.armed = False
        index.rebuild(_Repo(), SCOPE)  # type: ignore[arg-type]
        assert index.list_memory_ids(SCOPE) == {m.id for m in memories}
    finally:
        with index.engine.begin() as connection:
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}_meta"')
        index.close()
