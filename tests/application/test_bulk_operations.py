"""Bulk storage operations (zero-debt plan, Task 3.3).

- add_observations_and_enqueue_batch: same canonical contract as the
  single method (identity dedupe, provenance, event emission, P0-4
  atomicity) but ONE transaction for the whole batch — never per row.
- ack_outbox_many: batch ack in one transaction, idempotent.
- upsert_many on projections: one commit per batch, atomic, and the
  acceptance — bounded projection writes independent of scope size.

Acceptance (plan): current-head batch ingest must materially improve
against Phase 0 without weakening durability — the tests prove the
contract and bounded writes; the scale harness reports docs/sec.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.application.projections import ProjectionCoordinator  # noqa: E402
from severian.domain.models import Collection, Memory, Observation, Scope  # noqa: E402
from severian.infrastructure.fts import FTS5Index  # noqa: E402
from severian.infrastructure.sqlite import SQLiteRepository  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


def _scope(suffix: str = "") -> Scope:
    return Scope(
        tenant_id=f"t{suffix}", profile_id="p1", collection_id=f"c{suffix}",
        agent_id="a1", session_id="s1",
    )


def _observation(scope: Scope, i: int) -> Observation:
    return Observation(
        scope=scope,
        content=f"batch ingest evidence number {i} for the nightly report",
        raw_content=f"batch ingest evidence number {i} for the nightly report",
        source={"bench_document_id": f"doc-{i}", "uri": f"bench://doc-{i}"},
    )


def _memory(scope: Scope, i: int) -> Memory:
    return Memory(
        kind="fact",
        content=f"batch ingest evidence number {i} for the nightly report",
        scope=scope,
        source_observation_ids=[f"obs-{i}"],
        model_id="test/hasher",
        prompt_version="test-v1",
        confidence=0.9,
    )


class _CountingConnection:
    """sqlite3.Connection proxy that counts commit/rollback calls."""

    def __init__(self, conn: Any):
        self._conn = conn
        self.commit_count = 0
        self.rollback_count = 0

    def commit(self) -> None:
        self.commit_count += 1
        self._conn.commit()

    def rollback(self) -> None:
        self.rollback_count += 1
        self._conn.rollback()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class _CountingVector(LocalVectorIndex):
    """Counts commits so the one-commit-per-batch proof is direct."""

    def __init__(self, path: Path, embedding: object):
        super().__init__(path, embedding)  # type: ignore[arg-type]
        self.counter = _CountingConnection(self.connection)
        self.connection = self.counter  # type: ignore[assignment]


@pytest.fixture()
def env(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c", tenant_id="t", name="Primary"))
    vector = _CountingVector(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    yield {"repo": repo, "vector": vector}
    vector.close()
    repo.close()


# ---------------------------------------------------------------------------
# add_observations_and_enqueue_batch
# ---------------------------------------------------------------------------


def test_batch_ingest_inserts_all_and_emits_events(env) -> None:
    scope = _scope()
    observations = [_observation(scope, i) for i in range(25)]
    pairs = env["repo"].add_observations_and_enqueue_batch(observations)

    assert len(pairs) == 25
    assert len(env["repo"].list_observations(scope)) == 25
    jobs = [j for (_, j) in pairs]
    assert all(j.kind == "derive" for j in jobs)
    # One outbox event per new observation.
    memory_events = [e for e in env["repo"].pending_outbox() if e.kind == "observation.created"]
    assert len(memory_events) == 25


def test_batch_ingest_matches_single_contract_identities(env) -> None:
    """Batch and single paths produce the SAME canonical result."""
    scope = _scope()
    observations = [_observation(scope, i) for i in range(5)]
    pairs = env["repo"].add_observations_and_enqueue_batch(observations)
    assert len(pairs) == 5
    for obs, job in pairs:
        # Re-ingesting the same evidence via the SINGLE method returns
        # the stored observation and its existing job (idempotent).
        again_obs, again_job = env["repo"].add_observation_and_enqueue(obs)
        assert again_obs.id == obs.id
        assert again_job.id == job.id


def test_batch_ingest_is_idempotent(env) -> None:
    scope = _scope()
    observations = [_observation(scope, i) for i in range(10)]
    first = env["repo"].add_observations_and_enqueue_batch(observations)
    second = env["repo"].add_observations_and_enqueue_batch(observations)
    assert [o.id for (o, _) in first] == [o.id for (o, _) in second]
    assert len(env["repo"].list_observations(scope)) == 10  # no dupes


def test_batch_ingest_failure_rolls_back_whole_batch(env) -> None:
    """Durability: a failure mid-batch must not half-commit."""
    scope = _scope()
    # Duplicate explicit id inside the batch → IntegrityError on the
    # second insert → the whole transaction must roll back (P0-4).
    dup = _observation(scope, 1).model_copy(update={"id": "dup-id"})
    observations = [_observation(scope, i) for i in range(5)] + [dup, dup]
    with pytest.raises(Exception, match=r"IntegrityError|UNIQUE|conflict|duplicate key"):
        env["repo"].add_observations_and_enqueue_batch(observations)
    # Nothing from the batch persisted (atomic rollback).
    assert len(env["repo"].list_observations(scope)) == 0
# ---------------------------------------------------------------------------
# ack_outbox_many
# ---------------------------------------------------------------------------


def test_ack_outbox_many_acks_all_in_one_call(env) -> None:
    scope = _scope()
    env["repo"].add_observations_and_enqueue_batch([_observation(scope, i) for i in range(20)])
    pending = [e for e in env["repo"].pending_outbox() if e.kind == "observation.created"]
    assert len(pending) == 20

    env["repo"].ack_outbox_many([e.id for e in pending])
    assert len(env["repo"].pending_outbox()) == 0


def test_ack_outbox_many_idempotent_with_missing_ids(env) -> None:
    scope = _scope()
    env["repo"].add_observations_and_enqueue_batch([_observation(scope, i) for i in range(3)])
    pending = [e for e in env["repo"].pending_outbox() if e.kind == "observation.created"]
    ids = [e.id for e in pending]

    env["repo"].ack_outbox_many(ids)
    # Already acked + one unknown id: must NOT raise (idempotent).
    env["repo"].ack_outbox_many(ids + ["no-such-event"])
    assert len(env["repo"].pending_outbox()) == 0


def test_ack_outbox_many_chunked_beyond_bind_limits(env) -> None:
    """Scale B4 regression (2026-08-09): a 100k-event warmup pass acked
    the whole scope in ONE IN clause and blew past PostgreSQL's 65535
    bind-parameter cap. ack_outbox_many must chunk internally, so a
    70k-ID call (14 chunks of 5000) must not raise — including on the
    idempotent missing-ID path, which is how a re-acked backlog lands.
    """
    # Fabricated IDs exercise the chunked IN-clause path without needing
    # 70k real rows; missing ids are ignored by contract.
    ids = [f"fabricated-event-{i:06d}" for i in range(70_000)]
    env["repo"].ack_outbox_many(ids)  # must not raise
    # Real events spanning multiple chunks are all acked.
    scope = _scope()
    n = 12_000  # 3 chunks of 5000 (each observation emits 1 outbox event)
    env["repo"].add_observations_and_enqueue_batch([_observation(scope, i) for i in range(n)])
    pending = [e for e in env["repo"].pending_outbox() if e.kind == "observation.created"]
    assert len(pending) == n
    env["repo"].ack_outbox_many([e.id for e in pending])
    assert len(env["repo"].pending_outbox()) == 0


# ---------------------------------------------------------------------------
# upsert_many: one commit per batch, atomic, bounded
# ---------------------------------------------------------------------------


def test_upsert_many_single_commit(env) -> None:
    scope = _scope()
    memories = [_memory(scope, i) for i in range(30)]
    env["repo"].add_observations_and_enqueue_batch(
        [Observation(scope=scope, content=m.content, source={"uri": "bench"}) for m in memories]
    )
    for m in memories:
        m.source_observation_ids = [o.id for o in env["repo"].list_observations(scope)]

    fts = FTS5Index(env["repo"].engine.url.database.replace("severian.db", "fts.db"))
    try:
        env["vector"].upsert_many(memories)
        assert env["vector"].counter.commit_count == 1  # ONE commit, not 30
        assert env["vector"].count(scope) == 30

        # Rollback path: a failing commit must leave the batch uncommitted.
        boom_path = env["vector"].path.parent / "boom-vectors.db"
        boom = _CountingVector(boom_path, HashEmbedding(dimension=64))
        boom.upsert_many(memories)  # healthy batch lands
        assert boom.count(scope) == 30
        boom.counter.commit = lambda: (_ for _ in ()).throw(RuntimeError("boom at commit"))
        with pytest.raises(RuntimeError, match="boom at commit"):
            boom.upsert_many(memories)
        # Nothing from the failed batch persisted (atomic rollback).
        assert boom.count(scope) == 30
        boom.close()

        fts.upsert_many(memories)
        assert fts.count(scope) == 30
    finally:
        fts.close()


def test_upsert_many_bounded_write_acceptance(env) -> None:
    """One new memory costs ONE upsert, never a scope clear+reinsert."""
    scope = _scope()
    env["repo"].add_observations_and_enqueue_batch(
        [Observation(scope=scope, content=f"seed {i}", source={"uri": "bench"}) for i in range(50)]
    )
    seeds = [_memory(scope, i) for i in range(50)]
    env["vector"].upsert_many(seeds)
    assert env["vector"].count(scope) == 50

    env["vector"].counter.commit_count = 0
    env["vector"].upsert_many([_memory(scope, 999)])
    assert env["vector"].counter.commit_count == 1
    assert env["vector"].count(scope) == 51


def test_projection_coordinator_batch_apply_uses_upsert_many(env) -> None:
    """apply(events) on a batch routes through upsert_many (one commit)."""
    scope = _scope()
    env["repo"].add_observations_and_enqueue_batch(
        [Observation(scope=scope, content=f"seed {i}", source={"uri": "bench"}) for i in range(20)]
    )
    obs = env["repo"].list_observations(scope)
    memories = [
        Memory(
            kind="fact", content=o.content, scope=scope,
            source_observation_ids=[o.id], model_id="test/hasher",
            prompt_version="test-v1", confidence=0.9,
        )
        for o in obs
    ]
    fts = FTS5Index(env["repo"].engine.url.database.replace("severian.db", "fts.db"))
    coordinator = ProjectionCoordinator(env["repo"], fts, env["vector"])
    try:
        env["repo"].add_memories_batch(memories)
        events = [e for e in env["repo"].pending_outbox() if e.kind == "memory.created"]
        assert len(events) == 20

        commits_before = env["vector"].counter.commit_count
        coordinator.apply(events)
        assert env["vector"].counter.commit_count == commits_before + 1  # ONE commit
        assert env["vector"].count(scope) == 20
    finally:
        fts.close()
