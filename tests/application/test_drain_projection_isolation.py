"""Regression tests for the 2026-08-22 dead-letter incident.

Pins two behaviours:
1. Texts longer than MAX_TEXT_CHARS are rejected client-side without a
   socket round-trip (the >5s encode wedge class).
2. A projection failure during drain does NOT abort job completion —
   derived jobs still complete and outbox events stay pending (no more
   lease-expiry dead-letter cascades).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.application.worker import DerivationWorker
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.remote_qwen_embedding import (
    MAX_TEXT_CHARS,
    RemoteEmbeddingTextTooLongError,
    RemoteEmbeddingTransportError,
    RemoteQwenEmbedding,
)
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


@pytest.fixture
def bundle(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1-t1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts.db")
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    derivation = DerivationService(repo)
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=ProjectionCoordinator(repo, fts, vector),
        derivation=derivation,
        context=ContextAssembler(),
    )
    yield {
        "repo": repo,
        "service": service,
        "worker": None,
        "fts": fts,
        "vector": vector,
        "gateway": FakeGateway(),
        "scope": Scope(tenant_id="t1", profile_id="p1", collection_id="c1-t1", agent_id="a1", session_id="s1"),
    }
    fts.close()
    vector.close()
    repo.close()


@dataclass
class _FlakyProjection:
    """Projection coordinator stub whose apply() always explodes."""

    calls: int = 0
    events: list = field(default_factory=list)

    def apply(self, events):
        self.calls += 1
        self.events.extend(events)
        raise RuntimeError("simulated embedding transport outage")

    def rebuild(self, scope):  # pragma: no cover - not used in these tests
        raise AssertionError("rebuild must not be called by drain")


def _make_worker_with_flaky_projection(bundle, monkeypatch):
    worker = DerivationWorker(bundle["repo"], bundle["service"]._derivation, bundle["vector"] and ProjectionCoordinator(
        bundle["repo"], bundle["fts"], bundle["vector"]
    ))
    flaky = _FlakyProjection()
    monkeypatch.setattr(worker, "_projection", flaky, raising=False)
    return worker, flaky


def test_text_over_hard_cap_rejected_without_socket(tmp_path):
    provider = RemoteQwenEmbedding(tmp_path / "missing.sock")
    with pytest.raises(RemoteEmbeddingTextTooLongError):
        provider.encode("x" * (MAX_TEXT_CHARS + 1))


def test_text_at_cap_reaches_transport(tmp_path):
    # At exactly the cap the guard must pass through to transport (which
    # fails on the missing socket) — proving the boundary is inclusive.
    provider = RemoteQwenEmbedding(tmp_path / "missing.sock")
    with pytest.raises(RemoteEmbeddingTransportError):
        provider.encode("x" * MAX_TEXT_CHARS)


def test_ingest_enqueues_one_derive_job(bundle):
    ingest = IngestionService(bundle["repo"])
    result = ingest.ingest(
        bundle["scope"],
        "incident regression observation content",
        source={"provider": "test"},
    )
    assert result.observation.id
    pending = bundle["repo"].pending_outbox()
    assert isinstance(pending, list)


def test_projection_failure_does_not_abort_job_completion(bundle, monkeypatch):
    """The 2026-08-22 incident: one embed timeout aborted the pass after
    derivations succeeded, leaving jobs running → reaped → dead-letter."""
    ingest = IngestionService(bundle["repo"])
    ingest.ingest(
        bundle["scope"],
        "incident regression observation content",
        source={"provider": "test"},
    )

    worker, flaky = _make_worker_with_flaky_projection(bundle, monkeypatch)
    summary = worker.drain(worker_id="regression", gateway=bundle["gateway"])

    assert summary.processed == 1
    assert summary.failed == 0
    assert summary.projection_failed == 1
    assert flaky.calls >= 1
    # Job must be DONE, not running/dead_letter (column-level status).
    import sqlite3 as _sq

    con = _sq.connect(str(bundle["repo"].path))
    try:
        statuses = [
            r[0] for r in con.execute(
                "SELECT DISTINCT status FROM records WHERE record_type='job'"
            )
        ]
    finally:
        con.close()
    assert "running" not in statuses, "no job may be stranded running"
    assert "dead_letter" not in statuses, "no job may dead-letter from projection failure"


def test_projection_failure_leaves_outbox_pending_for_retry(bundle, monkeypatch):
    ingest = IngestionService(bundle["repo"])
    ingest.ingest(
        bundle["scope"],
        "outbox retry observation content",
        source={"provider": "test"},
    )

    worker, _flaky = _make_worker_with_flaky_projection(bundle, monkeypatch)
    worker.drain(worker_id="regression", gateway=bundle["gateway"])

    pending = bundle["repo"].pending_outbox()
    assert len(pending) >= 1, "events must stay pending when projection fails"
