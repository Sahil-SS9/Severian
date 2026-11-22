"""0.6.4 H: the audit reports vector coverage per scope and flags coverage under 99%."""

from __future__ import annotations

import dataclasses
import sqlite3
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


# -- H: coverage in the audit ---------------------------------------------------------------------


def test_audit_reports_vector_coverage_and_flags_low_scopes(store: Any) -> None:
    from severian.infrastructure.audit import audit_store

    _seed(store, [f"coverage memory {i}" for i in range(10)])
    # Simulate the incident: most vectors vanish.
    with sqlite3.connect(store.path / "vectors.db") as connection:
        ids = [r[0] for r in connection.execute("SELECT memory_id FROM memory_vectors ORDER BY memory_id")]
        connection.executemany("DELETE FROM memory_vectors WHERE memory_id = ?", [(i,) for i in ids[:8]])
    report = audit_store(store.path / "severian.db", store.path / "fts.db", store.path / "vectors.db")
    body = report.to_dict()
    coverage = body["vector_coverage"]
    assert coverage["active_memories"] == 10
    assert coverage["vector_rows"] == 2
    assert coverage["missing"] == 8
    assert coverage["coverage"] == pytest.approx(0.2)
    label = "/".join(SCOPE.model_dump().values())
    assert coverage["low_scopes"][label]["missing"] == 8
    assert "low_vector_coverage" in body["findings_by_kind"]


def test_audit_full_coverage_is_not_flagged(store: Any) -> None:
    from severian.infrastructure.audit import audit_store

    _seed(store, [f"healthy memory {i}" for i in range(3)])
    body = audit_store(store.path / "severian.db", store.path / "fts.db", store.path / "vectors.db").to_dict()
    assert body["vector_coverage"]["coverage"] == 1.0
    assert body["vector_coverage"]["low_scopes"] == {}
    assert "low_vector_coverage" not in body["findings_by_kind"]



def test_audit_counts_units_in_unit_fts_and_flags_unlabelled_unit_vectors_and_failed_events(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from severian.application.message_units import migrate_message_units
    from severian.infrastructure.audit import audit_store

    for turn in range(1, 3):
        store.service.ingest_observation(
            SCOPE, content=f"[2024/03/1{turn} (Fri)] user: question {turn}\nassistant: answer {turn}",
            source={"turn": turn},
        )
    store.worker.drain(worker_id="seed", gateway=FakeGateway())
    monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
    migrate_message_units(store.repo, SCOPE, confirm=True, rebuild=store.service._projection)
    monkeypatch.delenv("SEVERIAN_MESSAGE_UNITS")
    store.worker.drain(worker_id="settle", gateway=FakeGateway())
    paths = (store.path / "severian.db", store.path / "fts.db", store.path / "vectors.db")
    body = audit_store(*paths).to_dict()
    assert body["ok"] is True, body["findings"]
    assert body["vector_coverage"]["coverage"] == 1.0
    with sqlite3.connect(store.path / "vectors.db") as connection:
        connection.execute("UPDATE memory_vectors SET kind = '' WHERE kind = 'message_unit'")
    with sqlite3.connect(store.path / "severian.db") as connection:
        connection.execute(
            "UPDATE records SET status = 'failed' "
            "WHERE id = (SELECT id FROM records WHERE record_type = 'outbox' LIMIT 1)"
        )
    body = audit_store(*paths).to_dict()
    assert body["findings_by_kind"].get("unlabelled_unit_vector") == 1
    assert body["findings_by_kind"].get("failed_outbox_event") == 1
    assert body["jobs"]["failed_outbox"] == 1
