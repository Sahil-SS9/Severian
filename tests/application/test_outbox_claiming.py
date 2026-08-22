"""Concurrency proof for claim_outbox (HG follow-up, 2026-08-22).

Invariants pinned here (regardless of which thread wins):
- DISJOINTNESS: no event is ever claimed by two workers.
- COMPLETENESS: every event ends either claimed or pending — none vanish.
Self-contained fixture (no conftest)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


@pytest.fixture
def bundle(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1-t1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts")
    vector = LocalVectorIndex(tmp_path / "vec", HashEmbedding(dimension=64))
    derivation = DerivationService(repo)
    BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=ProjectionCoordinator(repo, fts, vector),
        derivation=derivation,
        context=ContextAssembler(),
    )
    return {
        "repo": repo,
        "scope": Scope(
            tenant_id="t1", profile_id="p1", collection_id="c1-t1",
            agent_id="a1", session_id="s1",
        ),
    }


def test_alternating_claims_are_disjoint_and_complete(bundle):
    """Deterministic alternation: each claim round is a separate call, so
    both workers participate. Proves disjointness + completeness."""
    ingest = IngestionService(bundle["repo"])
    for i in range(6):
        ingest.ingest(bundle["scope"], f"concurrency seed {i}",
                      source={"provider": "test"})

    results: dict[str, set[str]] = {"w1": set(), "w2": set()}
    for i in range(6):
        w = "w1" if i % 2 == 0 else "w2"
        claimed = bundle["repo"].claim_outbox(w, limit=1)
        assert len(claimed) == 1, f"round {i}: expected exactly one claim"
        results[w].update(e.id for e in claimed)

    w1, w2 = results["w1"], results["w2"]
    # DISJOINTNESS: no event claimed by BOTH workers.
    assert not (w1 & w2), "an event was claimed by BOTH workers"
    assert len(w1) == 3 and len(w2) == 3
    total_pending = {e.id for e in bundle["repo"].pending_outbox()}
    assert not (w1 | w2) & total_pending, "claimed events leaked to pending"
    # COMPLETENESS: every seeded event is claimed exactly once.
    with bundle["repo"].engine.connect() as conn:
        from sqlalchemy import select

        from severian.infrastructure.sqlite import records
        all_ids = {r[0] for r in conn.execute(
            select(records.c.id).where(records.c.record_type == "outbox"))}
    assert (w1 | w2) | total_pending == all_ids, "events lost between states"


def test_parallel_claims_never_double_claim(bundle):
    """Unsynchronised hammering: N threads loop-claim until empty.
    Disjointness and completeness must hold regardless of scheduling."""
    ingest = IngestionService(bundle["repo"])
    for i in range(12):
        ingest.ingest(bundle["scope"], f"hammer seed {i}",
                      source={"provider": "test"})

    workers = [f"w{i}" for i in range(4)]
    results: dict[str, set[str]] = {w: set() for w in workers}
    errors: dict[str, str] = {}

    def claim_loop(worker_id: str) -> None:
        while True:
            try:
                claimed = bundle["repo"].claim_outbox(worker_id, limit=3)
            except Exception as exc:
                errors[worker_id] = repr(exc)
                return
            if not claimed:
                return
            results[worker_id].update(e.id for e in claimed)

    threads = [threading.Thread(target=claim_loop, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"claim raised in worker thread: {errors}"
    seen: set[str] = set()
    for w in workers:
        dupes = seen & results[w]
        assert not dupes, f"{w} double-claimed events {dupes}"
        seen |= results[w]
    total_pending = {e.id for e in bundle["repo"].pending_outbox()}
    with bundle["repo"].engine.connect() as conn:
        from sqlalchemy import select

        from severian.infrastructure.sqlite import records
        all_ids = {r[0] for r in conn.execute(
            select(records.c.id).where(records.c.record_type == "outbox"))}
    assert seen | total_pending == all_ids, "events lost between states"
    assert len(seen) == 12, f"expected all 12 claimed once, got {len(seen)}"


def test_stale_claim_reaped_to_pending(bundle):
    """A crashed worker's projecting events return to pending via reap."""
    from datetime import UTC, datetime, timedelta

    ingest = IngestionService(bundle["repo"])
    ingest.ingest(bundle["scope"], "reap seed", source={"provider": "test"})

    claimed = bundle["repo"].claim_outbox("crashed-worker", limit=100,
                                          lease_seconds=60)
    assert claimed, "seed must produce claims"
    ids = {e.id for e in claimed}
    assert bundle["repo"].pending_outbox() == [], "claims must leave pending"

    # Reap BEFORE lease expiry: claims hold.
    bundle["repo"].reap_expired_jobs()
    still_pending = {e.id for e in bundle["repo"].pending_outbox()}
    assert not (ids & still_pending), "unexpired claim was reaped early"

    # Reap AFTER expiry: stale claims return to pending.
    future = datetime.now(UTC) + timedelta(seconds=120)
    bundle["repo"].reap_expired_jobs(now=future)
    back = {e.id for e in bundle["repo"].pending_outbox()}
    assert ids <= back, "expired claims were not reaped to pending"
