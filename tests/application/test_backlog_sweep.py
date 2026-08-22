"""Sweep + bisection regression tests (cor-001/cor-002 from simplify-swarm)."""
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
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


class FakeGateway:
    model_id = "local/test-model"
    prompt_version = "prompt-v1"
    is_cloud = False

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
        "fts": fts,
        "vector": vector,
        "gateway": FakeGateway(),
        "scope": Scope(tenant_id="t1", profile_id="p1",
                       collection_id="c1-t1", agent_id="a1", session_id="s1"),
    }
    fts.close()
    vector.close()
    repo.close()


@dataclass
class _SelectiveProjection:
    """Fails apply() only when a marked poison event id is present."""

    poison_ids: set[str]
    calls: list[list] = field(default_factory=list)

    def apply(self, events):
        self.calls.append(list(events))
        for e in events:
            if e.id in self.poison_ids:
                raise RuntimeError(f"poison event {e.id}")


def _make_worker(bundle, projection):
    worker = DerivationWorker(
        bundle["repo"],
        bundle["service"]._derivation,
        ProjectionCoordinator(bundle["repo"], bundle["fts"], bundle["vector"]),
    )
    worker._projection = projection
    return worker


def _seed_and_drain_once(bundle):
    """Ingest + derive one memory so its outbox events exist and jobs are done."""
    ingest = IngestionService(bundle["repo"])
    ingest.ingest(bundle["scope"], "backlog sweep regression content",
                  source={"provider": "test"})
    worker = DerivationWorker(
        bundle["repo"], bundle["service"]._derivation,
        ProjectionCoordinator(bundle["repo"], bundle["fts"], bundle["vector"]),
    )
    return worker.drain(worker_id="seed", gateway=bundle["gateway"])


def test_sweep_failure_sets_projection_failed(bundle, monkeypatch):
    """cor-001: an idle-pass sweep failure MUST surface in the summary —
    the drain cron alarms on projection_failed > 0."""
    seed = _seed_and_drain_once(bundle)
    assert seed.processed == 1
    # Force events back to pending (simulating a stranded backlog).
    with bundle["repo"].engine.begin() as conn:
        from sqlalchemy import select, update

        from severian.infrastructure.sqlite import records
        ids = [r[0] for r in conn.execute(
            select(records.c.id).where(records.c.record_type == "outbox"))]
        conn.execute(update(records).where(records.c.id.in_(ids))
                     .values(status="pending"))

    flaky = _SelectiveProjection(poison_ids=set())
    # First call fails (transport outage), later calls would succeed.
    calls = {"n": 0}
    orig_apply = flaky.apply

    def failing_apply(events):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated outage")
        return orig_apply(events)

    flaky.apply = failing_apply
    worker = _make_worker(bundle, flaky)
    summary = worker.drain(worker_id="sweep-regression", gateway=bundle["gateway"])
    # The batch-level failure triggers bisection; both halves then succeed,
    # so the SAME pass converges and no failure may be flagged.
    assert calls["n"] >= 2, "bisection must retry the batch"
    assert summary.projection_failed == 0
    assert bundle["repo"].pending_outbox() == [], "bisected retry converges"

    # Now the persistent-failure case: sweep fails outright and MUST flag.
    calls["n"] = 0

    def always_fail(events):
        calls["n"] += 1
        raise RuntimeError("simulated outage")

    flaky.apply = always_fail
    with bundle["repo"].engine.begin() as conn:
        ids = [r[0] for r in conn.execute(
            select(records.c.id).where(records.c.record_type == "outbox"))]
        conn.execute(update(records).where(records.c.id.in_(ids))
                     .values(status="pending"))
    summary_fail = worker.drain(worker_id="sweep-regression-3", gateway=bundle["gateway"])
    assert summary_fail.projection_failed == 1, "persistent sweep failure must flag"
    assert bundle["repo"].pending_outbox() != [], "failed sweep leaves events pending"

    # Recovery pass: apply works again, sweep converges the backlog.
    flaky.apply = orig_apply
    summary_ok = worker.drain(worker_id="sweep-regression-4", gateway=bundle["gateway"])
    assert summary_ok.projection_failed == 0
    assert bundle["repo"].pending_outbox() == [], "second pass must converge the backlog"


def test_poison_event_isolated_by_bisection(bundle):
    """cor-002: one poison event must not wedge the whole backlog — good
    events in the same batch still converge and get acked."""
    seed = _seed_and_drain_once(bundle)
    assert seed.processed == 1
    with bundle["repo"].engine.begin() as conn:
        from sqlalchemy import select, update

        from severian.infrastructure.sqlite import records
        rows = conn.execute(
            select(records.c.id).where(records.c.record_type == "outbox")
        ).fetchall()
        ids = [r[0] for r in rows]
        conn.execute(update(records).where(records.c.id.in_(ids))
                     .values(status="pending"))

    # Mark exactly ONE pending event as poison.
    pending = bundle["repo"].pending_outbox()
    assert len(pending) >= 1
    flaky = _SelectiveProjection(poison_ids={pending[0].id})
    worker = _make_worker(bundle, flaky)

    summary = worker.drain(worker_id="poison-regression", gateway=bundle["gateway"])
    assert summary.projection_failed == 1, "poison must flag"
    remaining = {e.id for e in bundle["repo"].pending_outbox()}
    assert remaining == {pending[0].id}, (
        "only the poison event may remain pending; all others converge+ack"
    )
