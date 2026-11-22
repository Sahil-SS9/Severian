"""0.6.4 C: the drain acks only outbox events whose projection converged.

0.6.3 acked the WHOLE claimed batch when bisection isolated a poison event, so the memory got no
vector and no alarm. Now the event stays pending for retry and, after OUTBOX_MAX_ATTEMPTS, lands
in a visible ``failed`` outbox state that ``severian outbox --requeue`` puts back.
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


# -- C: ack only what converged ---------------------------------------------------------------


def _outbox_status(repo: SQLiteRepository) -> dict[str, int]:
    from sqlalchemy import func, select

    from severian.infrastructure.sqlite import records

    with repo.engine.connect() as connection:
        rows = connection.execute(
            select(records.c.status, func.count()).where(records.c.record_type == "outbox").group_by(records.c.status)
        ).all()
    return {str(status): int(count) for status, count in rows}


def test_drain_does_not_ack_a_poison_event_with_its_batch(store: Any) -> None:
    """worker._project_outbox used to ack the WHOLE claimed batch when bisection isolated a poison event."""
    store.embedding.armed = True
    for i in range(4):
        store.service.ingest_observation(SCOPE, content=f"ordinary turn {i}", source={"provider": "test"})
    store.service.ingest_observation(SCOPE, content=f"{POISON} turn", source={"provider": "test"})
    summary = store.worker.drain(worker_id="w1", gateway=FakeGateway())
    assert summary.projection_failed == 1
    active = {m.id: m for m in store.repo.list_memories(SCOPE)}
    poison_ids = {mid for mid, m in active.items() if POISON in m.content}
    assert poison_ids
    assert store.vector.list_memory_ids(SCOPE) == set(active) - poison_ids
    pending_ids = {str(e.payload.get("id")) for e in store.repo.pending_outbox()}
    assert poison_ids <= pending_ids, "the poison memory's event must stay visible, not be acked"


def test_poison_event_is_dead_lettered_after_its_attempts_and_requeue_recovers(store: Any) -> None:
    from severian.application.worker import OUTBOX_MAX_ATTEMPTS

    store.embedding.armed = True
    store.service.ingest_observation(SCOPE, content=f"{POISON} turn", source={"provider": "test"})
    store.service.ingest_observation(SCOPE, content="fine turn", source={"provider": "test"})
    store.worker.drain(worker_id="w1", gateway=FakeGateway())
    for attempt in range(OUTBOX_MAX_ATTEMPTS + 1):
        store.worker.drain(worker_id=f"w{attempt + 2}", gateway=FakeGateway())
    failed = store.repo.failed_outbox()
    assert failed, "a persistently failing event ends visible in the failed outbox state"
    assert all(POISON in store.repo.get_memory(SCOPE, str(e.payload["id"])).content for e in failed)
    assert store.repo.pending_outbox() == [], "a dead-lettered event no longer loops"
    assert _outbox_status(store.repo).get("failed", 0) == len(failed)
    store.embedding.armed = False
    assert store.repo.requeue_failed_outbox() == len(failed)
    summary = store.worker.drain(worker_id="recover", gateway=FakeGateway())
    assert summary.projection_failed == 0
    assert store.repo.failed_outbox() == []
    assert store.vector.list_memory_ids(SCOPE) == {m.id for m in store.repo.list_memories(SCOPE)}


def test_transient_failure_is_retried_not_acked(store: Any) -> None:
    store.embedding.armed = True
    store.service.ingest_observation(SCOPE, content=f"{POISON} once", source={"provider": "test"})
    store.worker.drain(worker_id="w1", gateway=FakeGateway())
    assert store.vector.list_memory_ids(SCOPE) == set()
    store.embedding.armed = False  # the sidecar came back
    summary = store.worker.drain(worker_id="w2", gateway=FakeGateway())
    assert summary.projection_failed == 0
    assert store.vector.list_memory_ids(SCOPE) == {m.id for m in store.repo.list_memories(SCOPE)}
