"""Tests for the durable derivation worker.

The regression this guards: observations were ingested and a ``derive``
job enqueued, but no worker consumed the queue — so memories were never
promoted and default search returned empty. These tests prove the
worker closes that gap: claim → derive → complete → rebuild → searchable.
"""
from __future__ import annotations

from dataclasses import dataclass
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
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


@dataclass
class FailingGateway:
    """Gateway whose derive always fails — used to prove failed jobs do not wedge the queue."""

    model_id: str = "local/fail-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        raise RuntimeError("gateway boom")


def _scope(tenant: str = "t1") -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id="p1",
        collection_id="c1",
        agent_id="a1",
        session_id="s1",
    )


@pytest.fixture
def bundle(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
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
    worker = DerivationWorker(repo, derivation, ProjectionCoordinator(repo, fts, vector))
    yield {"repo": repo, "service": service, "worker": worker, "fts": fts, "vector": vector, "gateway": FakeGateway()}
    fts.close()
    vector.close()
    repo.close()


def test_worker_promotes_observation_to_searchable_memory(bundle):
    """THE regression: ingest then drain then search returns the memory."""
    view = bundle["service"].ingest_observation(
        _scope(), content="Ada Lovelace designed the first algorithm.", source={"uri": "note://ada"}
    )
    assert view.job_kind == "derive"

    # Before the worker runs, search returns nothing — the pre-fix behaviour.
    before = bundle["service"].search(_scope(), query="algorithm", limit=10)
    assert before == []

    summary = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert summary.processed == 1
    assert summary.failed == 0
    assert len(summary.touched_scopes) == 1

    hits = bundle["service"].search(_scope(), query="algorithm", limit=10)
    assert len(hits) == 1
    assert "Ada Lovelace" in hits[0].content
    assert hits[0].memory_id != view.observation_id  # derived memory, not raw observation
    assert hits[0].source_observation_ids == [view.observation_id]


def test_worker_drains_all_pending_jobs_in_order(bundle):
    sc = _scope()
    for i in range(5):
        bundle["service"].ingest_observation(sc, content=f"document number {i}", source={"uri": f"doc://{i}"})

    summary = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert summary.processed == 5
    assert summary.failed == 0

    hits = bundle["service"].search(sc, query="document", limit=50)
    assert len(hits) == 5


def test_worker_is_idempotent_on_repeat_drain(bundle):
    sc = _scope()
    bundle["service"].ingest_observation(sc, content="stable content", source={"uri": "note://stable"})
    first = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert first.processed == 1

    second = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert second.processed == 0  # queue empty

    hits = bundle["service"].search(sc, query="stable", limit=10)
    assert len(hits) == 1  # no duplicate memories from re-derivation


def test_worker_marks_failed_job_and_continues(bundle):
    sc = _scope()
    bundle["service"].ingest_observation(sc, content="doomed", source={"uri": "note://doomed"})
    bundle["service"].ingest_observation(sc, content="healthy", source={"uri": "note://ok"})

    # First pass with a failing gateway, bounded to one job: the doomed
    # job fails but is completed as failed — the queue is not wedged.
    summary = bundle["worker"].drain(worker_id="w1", gateway=FailingGateway(), max_jobs=1)
    assert summary.processed == 0
    assert summary.failed == 1

    # Second pass with a working gateway: healthy observation still derives.
    summary2 = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert summary2.processed == 1
    assert summary2.failed == 0

    hits = bundle["service"].search(sc, query="healthy", limit=10)
    assert len(hits) == 1


def test_worker_max_jobs_bounds_a_pass(bundle):
    sc = _scope()
    for i in range(5):
        bundle["service"].ingest_observation(sc, content=f"bounded {i}", source={"uri": f"b://{i}"})

    first = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"], max_jobs=2)
    assert first.processed == 2

    second = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"], max_jobs=2)
    assert second.processed == 2

    third = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert third.processed == 1


def test_worker_unknown_job_kind_is_failed_not_wedged(bundle):
    repo = bundle["repo"]
    sc = _scope()
    repo.enqueue_job(sc, "make-tea", {"observation_id": "x"})  # unsupported kind
    summary = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert summary.failed == 1
    assert summary.processed == 0

    # The bad job is gone from pending; the queue is not wedged.
    drained = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert drained.total == 0


def test_worker_scopes_are_respected(bundle):
    sc_a = _scope(tenant="t1")
    sc_b = _scope(tenant="t2")
    bundle["service"].ingest_observation(sc_a, content="alpha secret", source={"uri": "a://1"})
    bundle["service"].ingest_observation(sc_b, content="beta secret", source={"uri": "b://1"})

    summary = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    assert summary.processed == 2
    assert len(summary.touched_scopes) == 2

    hits_a = bundle["service"].search(sc_a, query="alpha", limit=10)
    hits_b = bundle["service"].search(sc_b, query="alpha", limit=10)
    assert len(hits_a) == 1
    # Scope isolation: no memory from scope A may appear in scope B results.
    assert all(hit.scope.tenant_id == "t2" for hit in hits_b)


def test_worker_recall_all_query_lists_every_memory_in_scope(bundle):
    """The admin graph's `q=*` (recall-all) must return every memory in scope."""
    sc = _scope()
    for i in range(4):
        bundle["service"].ingest_observation(sc, content=f"graph node {i}", source={"uri": f"g://{i}"})
    bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])

    hits = bundle["service"].search(sc, query="*", limit=200)
    assert len(hits) == 4
    assert all(hit.scope == sc for hit in hits)

    # Empty query behaves the same way.
    hits_empty = bundle["service"].search(sc, query="  ", limit=200)
    assert len(hits_empty) == 4


def test_worker_recall_all_is_scope_bounded(bundle):
    sc_a = _scope(tenant="t1")
    sc_b = _scope(tenant="t2")
    bundle["service"].ingest_observation(sc_a, content="only in A", source={"uri": "a://1"})
    bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])

    hits_a = bundle["service"].search(sc_a, query="*", limit=200)
    hits_b = bundle["service"].search(sc_b, query="*", limit=200)
    assert len(hits_a) == 1
    assert hits_b == []  # recall-all never leaks across scope
