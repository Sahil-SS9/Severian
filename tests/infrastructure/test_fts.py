"""FTS5 index tests — porter tokenizer schema migration (EVID-09)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from severian.infrastructure.fts import FTS5Index


def _old_schema_fts(path: Path) -> None:
    """Create an FTS5 table with the OLD (pre-porter) default unicode61
    tokenizer — exactly what a pre-2026-08-05 database looks like."""
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE VIRTUAL TABLE memory_fts USING fts5(
            memory_id UNINDEXED,
            tenant_id UNINDEXED,
            profile_id UNINDEXED,
            collection_id UNINDEXED,
            agent_id UNINDEXED,
            session_id UNINDEXED,
            content,
            kind,
            source_ids UNINDEXED
        )"""
    )
    conn.commit()
    conn.close()


def test_porter_tokenizer_migration_recreates_old_schema(tmp_path: Path) -> None:
    """EVID-09: opening an old-schema (unicode61) FTS database with the
    current code must migrate it to the porter tokenizer so inflected
    queries match (authentication → authenticated)."""
    path = tmp_path / "old-fts.db"
    _old_schema_fts(path)

    index = FTS5Index(path)
    # The migration runs in __init__/open; verify the DDL now carries porter.
    row = index.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
    ).fetchone()
    assert "porter" in (row[0] if row else "").casefold()
    index.close()


def test_porter_migration_makes_inflected_query_match(tmp_path: Path) -> None:
    """EVID-09: after migration, an inflected query (stemming) finds the
    base-form document — proving the porter tokenizer is functionally
    active, not just present in DDL."""
    path = tmp_path / "old-fts.db"
    _old_schema_fts(path)
    index = FTS5Index(path)  # migration happens here

    index.upsert(_memory("m1", "t1", "p1", "c1", "a1", "s1", "authentication is enforced"))
    index.upsert(_memory("m2", "t1", "p1", "c1", "a1", "s1", "the sky is blue"))

    from severian.domain.models import Scope

    scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    hits = index.search(scope, "authenticated", limit=10)
    ids = [h.memory_id for h in hits]
    assert "m1" in ids, f"porter stemming must match 'authenticated' -> 'authentication' (got {ids})"
    index.close()


def _memory(
    memory_id: str,
    tenant: str,
    profile: str,
    collection: str,
    agent: str,
    session: str,
    content: str,
):
    """Build the minimal Memory model for index.upsert."""
    from datetime import UTC, datetime

    from severian.domain.models import Memory, Scope

    return Memory(
        id=memory_id,
        scope=Scope(
            tenant_id=tenant,
            profile_id=profile,
            collection_id=collection,
            agent_id=agent,
            session_id=session,
        ),
        kind="fact",
        content=content,
        source_observation_ids=["obs-1"],
        model_id="test",
        prompt_version="test",
        confidence=0.9,
        created_at=datetime.now(UTC),
    )
