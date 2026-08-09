"""Atomic vector rebuild contract for PostgreSQL (zero-debt plan, Task 2.3).

Mirrors the sqlite rebuild contract (tests/unit/test_atomic_rebuild.py)
for the side-by-side PG table swap:

- Rebuild is side-by-side: live table untouched during build.
- Swap is transactional: live -> prev, candidate -> live, candidate meta
  -> vector_space_meta.
- Failure/abort before swap leaves the original untouched.
- Verification contract: count, digest, dimension/fingerprint, canary
  retrieval, deterministic ranking.

Skipped unless ``SEVERIAN_PG_DSN`` is set (live PostgreSQL required).
Uses ``PGVectorIndex.reset()`` to drop/recreate the tables around each
test — the same convention as test_embedding_space_identity.py live
tests. Tests run sequentially in the suite.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from sqlalchemy import create_engine, inspect  # noqa: E402

from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.pgvector_index import PGVectorIndex  # noqa: E402
from severian.infrastructure.rebuild import rebuild_pg_vectors  # noqa: E402
from severian.infrastructure.vector import HashEmbedding  # noqa: E402

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")

pytestmark = pytest.mark.skipif(
    not PG_DSN,
    reason="SEVERIAN_PG_DSN not set; skipping live PostgreSQL rebuild tests",
)


def _scope() -> Scope:
    return Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1",
        agent_id="a1", session_id="s1",
    )


class _MemoryRepository:
    """Duck-typed canonical store (rebuild only needs list_memories)."""

    def __init__(self, memories: list[Memory]):
        self._memories = memories

    def list_memories(self, scope: Scope) -> list[Memory]:
        return [
            m for m in self._memories
            if m.scope.tenant_id == scope.tenant_id
            and m.scope.profile_id == scope.profile_id
            and m.scope.collection_id == scope.collection_id
            and m.scope.agent_id == scope.agent_id
            and m.scope.session_id == scope.session_id
        ]


def _memories(n: int = 12, scope: Scope | None = None) -> list[Memory]:
    scope = scope or _scope()
    return [
        Memory(
            id=f"pg-mem-{i:03d}",
            kind="fact",
            content=f"the backup job for region {chr(97 + i % 26)} runs at {2 + i % 20:02d}:00 utc",
            scope=scope,
            source_observation_ids=[f"obs-{i:03d}"],
            model_id="test/hasher",
            prompt_version="test-v1",
            confidence=0.9,
        )
        for i in range(n)
    ]


def _reset_tables(dsn: str) -> None:
    """Drop live + any leftover swap artifacts so each test starts clean."""
    idx = PGVectorIndex(dsn, HashEmbedding(), table_name="memory_vectors")
    idx.reset()
    idx.close()
    engine = _engine(dsn)
    with engine.begin() as connection:
        rows = connection.exec_driver_sql(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND (tablename LIKE 'memory_vectors%%' OR tablename LIKE 'vector_space_meta%%')"
        ).fetchall()
        for (t,) in rows:
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    engine.dispose()


def _engine(dsn: str):
    """Project convention: normalise postgresql:// to psycopg3 dialect."""
    if dsn.startswith("postgresql://") and "+" not in dsn.split("://", 1)[0]:
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(dsn)


def _table_names(dsn: str) -> set[str]:
    engine = _engine(dsn)
    with engine.connect() as connection:
        names = set(inspect(connection).get_table_names())
    engine.dispose()
    return names


def test_rebuild_pg_vectors_swaps_and_verifies() -> None:
    scope = _scope()
    repo = _MemoryRepository(_memories())
    _reset_tables(PG_DSN)

    # Live index populated the normal way.
    live = PGVectorIndex(PG_DSN, HashEmbedding(), table_name="memory_vectors")
    live.initialize()
    for memory in repo.list_memories(scope):
        live.upsert(memory)
    live.close()

    result = rebuild_pg_vectors(PG_DSN, repo, scope, HashEmbedding())

    assert result.status == "swapped", result.error
    assert result.verification is not None
    assert result.verification.passed
    assert result.verification.canonical_count == 12
    assert result.verification.vector_count == 12
    assert result.verification.digest_match is True
    assert result.verification.deterministic is True
    assert result.rollback_path is not None

    # Live table is the rebuilt index and is usable; meta table swapped.
    reopened = PGVectorIndex(PG_DSN, HashEmbedding(), table_name="memory_vectors")
    assert reopened.count(scope) == 12
    reopened.close()

    tables = _table_names(PG_DSN)
    assert "memory_vectors" in tables
    assert "memory_vectors_meta" in tables
    assert result.rollback_path in tables  # prev table preserved as rollback
    assert f"{result.rollback_path}_meta" in tables  # prev meta preserved too


def test_rebuild_pg_vectors_no_keep_rollback() -> None:
    scope = _scope()
    repo = _MemoryRepository(_memories())
    _reset_tables(PG_DSN)

    live = PGVectorIndex(PG_DSN, HashEmbedding(), table_name="memory_vectors")
    live.initialize()
    for memory in repo.list_memories(scope):
        live.upsert(memory)
    live.close()

    result = rebuild_pg_vectors(PG_DSN, repo, scope, HashEmbedding(), keep_rollback=False)

    assert result.status == "swapped"
    assert result.rollback_path is None
    tables = _table_names(PG_DSN)
    assert "memory_vectors" in tables
    assert "memory_vectors_meta" in tables


def test_rebuild_pg_vectors_failure_leaves_original() -> None:
    scope = _scope()
    repo = _MemoryRepository(_memories())
    _reset_tables(PG_DSN)

    live = PGVectorIndex(PG_DSN, HashEmbedding(), table_name="memory_vectors")
    live.initialize()
    for memory in repo.list_memories(scope):
        live.upsert(memory)
    live.close()

    class _BoomRepository(_MemoryRepository):
        def list_memories(self, scope: Scope) -> list[Memory]:
            raise RuntimeError("boom")

    result = rebuild_pg_vectors(PG_DSN, _BoomRepository([]), scope, HashEmbedding())

    assert result.status == "failed"
    assert result.error is not None
    # Original untouched, no candidate/prev litter.
    tables = _table_names(PG_DSN)
    assert "memory_vectors" in tables
    assert "memory_vectors_meta" in tables
    assert not [t for t in tables if "_rebuild_" in t]
    assert not [t for t in tables if "_prev_" in t]
