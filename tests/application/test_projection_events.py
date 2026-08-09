"""Incremental projection events (zero-debt plan, Task 3.2).

The outbox is the projection delta contract. apply(events) touches only
the affected memory rows — never a scope-wide clear+reinsert — and is
idempotent under crash, restart, duplicate, out-of-order, supersession
and cross-scope delivery.

Crash tests cover:
- failure after FTS but before vector;
- failure after vector but before ack;
- repeated event application;
- process restart (events survive and re-apply);
- supersession/delete (revised → old head leaves projections);
- scope isolation;
- out-of-order or duplicate outbox delivery.

Acceptance: one new memory produces bounded projection writes
independent of scope size — a one-memory update must NOT clear/reinsert
the scope.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.application.projections import ProjectionCoordinator  # noqa: E402
from severian.domain.models import Collection, Event, Memory, Scope  # noqa: E402
from severian.infrastructure.fts import FTS5Index  # noqa: E402
from severian.infrastructure.sqlite import SQLiteRepository  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


def _scope(suffix: str = "") -> Scope:
    return Scope(
        tenant_id=f"t{suffix}", profile_id="p1", collection_id=f"c{suffix}",
        agent_id="a1", session_id="s1",
    )


def _memory(scope: Scope, i: int) -> Memory:
    return Memory(
        kind="fact",
        content=f"the backup job for region {chr(97 + i % 26)} runs at {2 + i % 20:02d}:00 utc",
        scope=scope,
        source_observation_ids=[f"obs-{i}"],
        model_id="test/hasher",
        prompt_version="test-v1",
        confidence=0.9,
    )


class _RecordingVector(LocalVectorIndex):
    """Records clear/upsert/delete calls for the bounded-write proof."""

    def __init__(self, path: Path, embedding: object):
        super().__init__(path, embedding)  # type: ignore[arg-type]
        self.clear_calls = 0
        self.upsert_calls = 0
        self.delete_calls = 0

    def clear(self, scope: Scope | None = None) -> None:
        self.clear_calls += 1
        super().clear(scope)

    def upsert(self, memory: Memory) -> None:
        self.upsert_calls += 1
        super().upsert(memory)

    def delete(self, memory_id: str) -> None:
        self.delete_calls += 1
        super().delete(memory_id)


@pytest.fixture()
def env(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c", tenant_id="t", name="Primary"))
    repo.create_collection(Collection(id="c2", tenant_id="t2", name="Second"))
    fts = FTS5Index(tmp_path / "fts.db")
    vector = _RecordingVector(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    coordinator = ProjectionCoordinator(repo, fts, vector)
    yield {
        "repo": repo, "fts": fts, "vector": vector,
        "coordinator": coordinator,
    }
    fts.close()
    vector.close()
    repo.close()


def _events_from_memories(repo: SQLiteRepository, memories: list[Memory], *, scope: Scope) -> list[Event]:
    """Add memories via the repo (emits outbox events) and return them."""
    from severian.domain.models import Observation

    # The canonical store validates that source observations exist;
    # create them with the EXACT ids the memories reference.
    for memory in memories:
        for source_id in memory.source_observation_ids:
            try:
                repo.get_observation(scope, source_id)
            except Exception:
                repo.add_observation(
                    Observation(id=source_id, scope=scope, content=memory.content)
                )
    added = repo.add_memories_batch(memories)
    assert len(added) == len(memories)
    # Only memory events carry projection deltas; observation events are
    # ignored by apply() (no projection impact) and filtered here so
    # count assertions are unambiguous.
    return [e for e in repo.pending_outbox() if e.scope == scope and e.kind.startswith("memory.")]


def test_apply_created_upserts_both_projections(env) -> None:
    scope = _scope()
    events = _events_from_memories(env["repo"], [_memory(scope, 1)], scope=scope)
    env["coordinator"].apply(events)

    assert env["fts"].count(scope) == 1
    assert env["vector"].count(scope) == 1
    # Bounded: exactly one upsert each, ZERO clears (acceptance).
    assert env["vector"].clear_calls == 0
    assert env["vector"].upsert_calls == 1


def test_one_memory_update_never_clears_scope(env) -> None:
    """Acceptance: bounded projection writes independent of scope size.

    Seed 50 memories and project them all; then a SINGLE new memory's
    event must not clear/reinsert anything — just one upsert.
    """
    scope = _scope()
    seed = [_memory(scope, i) for i in range(50)]
    seed_events = _events_from_memories(env["repo"], seed, scope=scope)
    env["coordinator"].apply(seed_events)
    for event in seed_events:
        env["repo"].ack_outbox(event.id)  # worker acks after converge
    assert env["vector"].count(scope) == 50

    env["vector"].clear_calls = 0
    env["vector"].upsert_calls = 0
    events = _events_from_memories(env["repo"], [_memory(scope, 999)], scope=scope)
    env["coordinator"].apply(events)

    assert env["vector"].count(scope) == 51
    assert env["vector"].clear_calls == 0
    assert env["vector"].upsert_calls == 1


def test_failure_after_fts_before_vector_is_retryable(env) -> None:
    """Crash between FTS and vector: nothing acked, retry converges."""
    scope = _scope()
    events = _events_from_memories(env["repo"], [_memory(scope, 1)], scope=scope)

    # First apply: FTS succeeds, vector raises mid-upsert.
    class _BoomVector(_RecordingVector):
        def upsert(self, memory: Memory) -> None:
            raise RuntimeError("crash after FTS")

    # Swap in the crashing vector on the SAME underlying db path.
    boom = _BoomVector(env["vector"].path, HashEmbedding(dimension=64))
    crashing = ProjectionCoordinator(env["repo"], env["fts"], boom)
    with pytest.raises(RuntimeError, match="crash after FTS"):
        crashing.apply(events)
    boom.close()

    # Crash = no ack; events still pending.
    assert len([e for e in env["repo"].pending_outbox() if e.kind.startswith("memory.")]) == 1

    # Retry with a healthy vector converges: FTS row already there,
    # upsert idempotent, vector row lands.
    env["coordinator"].apply(events)
    assert env["fts"].count(scope) == 1
    assert env["vector"].count(scope) == 1


def test_failure_after_vector_before_ack_is_retryable(env) -> None:
    """Crash after projection but before ack: event pending, re-apply no-ops."""
    scope = _scope()
    events = _events_from_memories(env["repo"], [_memory(scope, 1)], scope=scope)

    # Simulate: apply succeeded, but the worker crashed before acking.
    env["coordinator"].apply(events)
    pending = [e for e in env["repo"].pending_outbox() if e.kind.startswith("memory.")]
    assert len(pending) == 1  # still pending (no ack)

    # Re-apply (next pass after restart): idempotent, converges.
    env["coordinator"].apply(events)
    assert env["fts"].count(scope) == 1
    assert env["vector"].count(scope) == 1


def test_restart_survives_pending_events(env, tmp_path: Path) -> None:
    """Process restart: events live in the durable outbox, re-apply works."""
    scope = _scope()
    _events_from_memories(env["repo"], [_memory(scope, 1)], scope=scope)

    # "Restart": close everything, reopen on the same files.
    env["fts"].close()
    env["vector"].close()
    env["repo"].close()

    repo2 = SQLiteRepository(tmp_path / "severian.db")
    repo2.initialize()
    fts2 = FTS5Index(tmp_path / "fts.db")
    vector2 = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    coord2 = ProjectionCoordinator(repo2, fts2, vector2)

    pending = [
        e for e in repo2.pending_outbox()
        if e.scope == scope and e.kind.startswith("memory.")
    ]
    assert len(pending) == 1
    coord2.apply(pending)
    assert fts2.count(scope) == 1
    assert vector2.count(scope) == 1

    fts2.close()
    vector2.close()
    repo2.close()


def test_repeated_application_is_idempotent(env) -> None:
    scope = _scope()
    events = _events_from_memories(env["repo"], [_memory(scope, 1)], scope=scope)
    for _ in range(3):
        env["coordinator"].apply(events)
    assert env["fts"].count(scope) == 1
    assert env["vector"].count(scope) == 1
    assert env["vector"].upsert_calls == 3  # upserts, not clears


def test_duplicate_delivery_converges(env) -> None:
    scope = _scope()
    events = _events_from_memories(env["repo"], [_memory(scope, 1)], scope=scope)
    env["coordinator"].apply(events + events)  # duplicated in one batch
    assert env["fts"].count(scope) == 1
    assert env["vector"].count(scope) == 1


def test_revised_supersedes_old_head_in_projections(env) -> None:
    scope = _scope()
    original = _memory(scope, 1)
    events = _events_from_memories(env["repo"], [original], scope=scope)
    env["coordinator"].apply(events)
    assert env["vector"].count(scope) == 1

    # Revise: creates a revision and emits memory.revised.
    revision = env["repo"].revise_memory(
        scope, original.id, content="updated backup schedule",
        model_id="test/hasher", prompt_version="test-v1",
    )
    revised_events = [e for e in env["repo"].pending_outbox() if e.kind == "memory.revised"]
    assert len(revised_events) == 1
    env["coordinator"].apply(revised_events)

    # Old head gone, revision present — count stays 1, content updated.
    assert env["vector"].count(scope) == 1
    assert env["fts"].count(scope) == 1
    assert revision.id in [r.memory_id for r in env["vector"].search(scope, "updated backup schedule", limit=5)]


def test_rolled_back_restores_and_demotes(env) -> None:
    scope = _scope()
    original = _memory(scope, 1)
    events = _events_from_memories(env["repo"], [original], scope=scope)
    env["coordinator"].apply(events)

    env["repo"].revise_memory(
        scope, original.id, content="updated backup schedule",
        model_id="test/hasher", prompt_version="test-v1",
    )
    env["coordinator"].apply([e for e in env["repo"].pending_outbox() if e.kind == "memory.revised"])
    assert env["vector"].count(scope) == 1

    # Roll back to the original head.
    restored = env["repo"].rollback_memory(scope, original.id)
    rolled = [e for e in env["repo"].pending_outbox() if e.kind == "memory.rolled_back"]
    assert len(rolled) == 1
    env["coordinator"].apply(rolled)

    assert env["vector"].count(scope) == 1
    assert restored.id == original.id
    hits = env["vector"].search(scope, "backup job for region", limit=5)
    assert hits and hits[0].memory_id == original.id


def test_scope_isolation(env) -> None:
    scope_a = _scope("a")
    scope_b = _scope("b")
    events_a = _events_from_memories(env["repo"], [_memory(scope_a, 1)], scope=scope_a)
    events_b = _events_from_memories(env["repo"], [_memory(scope_b, 2)], scope=scope_b)
    assert len(events_b) == 1  # B has its own pending event

    # Apply ONLY scope A's events: scope B untouched.
    env["coordinator"].apply([e for e in events_a if e.scope == scope_a])
    assert env["vector"].count(scope_a) == 1
    assert env["vector"].count(scope_b) == 0


def test_out_of_order_delivery_converges(env) -> None:
    """Out-of-order events still converge: each event is self-contained."""
    scope = _scope()
    m1, m2 = _memory(scope, 1), _memory(scope, 2)
    events = _events_from_memories(env["repo"], [m1, m2], scope=scope)
    # Deliver reversed.
    env["coordinator"].apply(list(reversed(events)))
    assert env["vector"].count(scope) == 2
    assert env["fts"].count(scope) == 2
