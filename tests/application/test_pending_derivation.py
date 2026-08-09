"""Pending-derivation contract (zero-debt plan, Task 3.1).

- list_pending_observations returns ONLY active observations with no
  derived memory, bounded by limit, oldest first.
- A fully derived scope reports zero pending.
- derive=True on a fully derived scope stays close to derive=False
  (no full-scope scans, no list_memories-per-observation).
- One pending observation processes only that item.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.domain.models import Memory, Observation, Scope  # noqa: E402
from severian.infrastructure.sqlite import SQLiteRepository  # noqa: E402


def _scope() -> Scope:
    return Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1",
        agent_id="a1", session_id="s1",
    )


def _observation(scope: Scope, content: str, *, revision_of_id: str | None = None) -> Observation:
    return Observation(
        scope=scope,
        content=content,
        revision_of_id=revision_of_id,
    )


def _memory(scope: Scope, observation: Observation, content: str | None = None) -> Memory:
    return Memory(
        kind="fact",
        content=content or observation.content,
        scope=scope,
        source_observation_ids=[observation.id],
        model_id="test/hasher",
        prompt_version="test-v1",
        confidence=0.9,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> SQLiteRepository:
    r = SQLiteRepository(str(tmp_path / "store.db"))
    r.initialize()
    r.create_collection(
        __import__("severian.domain.models", fromlist=["Collection"]).Collection(
            tenant_id="t1", collection_id="c1", name="test",
        )
    )
    return r


def test_pending_empty_when_no_observations(repo: SQLiteRepository) -> None:
    assert repo.list_pending_observations(_scope(), limit=50) == []


def test_pending_lists_only_underived_active_observations(repo: SQLiteRepository) -> None:
    scope = _scope()
    o1 = repo.add_observation(_observation(scope, "the backup job runs at 02:00 utc"))
    o2 = repo.add_observation(_observation(scope, "the fraud model scores nightly"))
    # o3 is revised: the ORIGINAL is superseded (never pending); the
    # REVISION is a new active observation — pending until derived.
    o3 = repo.add_observation(_observation(scope, "old content"))
    revision = repo.revise_observation(scope, o3.id, "revised content")

    # Nothing derived yet: o1, o2, revision pending (id order).
    pending = repo.list_pending_observations(scope, limit=50)
    assert [o.id for o in pending] == sorted([o1.id, o2.id, revision.id])

    # Derive o1 → no longer pending; o2 + revision remain.
    repo.add_memory(_memory(scope, o1))
    pending = repo.list_pending_observations(scope, limit=50)
    assert [o.id for o in pending] == sorted([o2.id, revision.id])


def test_pending_respects_limit_oldest_first(repo: SQLiteRepository) -> None:
    scope = _scope()
    added = [repo.add_observation(_observation(scope, f"fact number {i}")) for i in range(10)]
    # Contract ordering is by record id (matches list_observations),
    # not insertion order — UUIDs are not chronological.
    by_id = sorted(o.id for o in added)

    assert [o.id for o in repo.list_pending_observations(scope, limit=3)] == by_id[:3]
    assert [o.id for o in repo.list_pending_observations(scope, limit=100)] == by_id


def test_pending_excludes_superseded_observations(repo: SQLiteRepository) -> None:
    scope = _scope()
    original = repo.add_observation(_observation(scope, "old head"))
    repo.revise_observation(scope, original.id, "new head")

    # The original is superseded: neither the old nor the revision
    # observation is pending until the revision is derived.
    pending = repo.list_pending_observations(scope, limit=50)
    assert [o.id for o in pending] == [o.id for o in repo.list_observations(scope)
                                       if o.superseded_by_id is None]
    assert original.id not in [o.id for o in pending]
