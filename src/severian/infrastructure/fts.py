from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Scope
from severian.infrastructure.vector import _STOPWORDS

_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)


def _restrict_private(path: str | Path) -> None:
    """Restrict a DB file and its WAL/SHM siblings to the owning user.

    Mirrors the canonical store's hardening (sqlite.py initialize):
    memory content is sensitive; FTS/vector projection files must not
    be world-readable (default umask 0644).
    """
    for candidate in (str(path), str(path) + "-wal", str(path) + "-shm"):
        try:
            os.chmod(candidate, 0o600)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class FTSHit:
    memory_id: str
    score: float
    snippet: str


class FTS5Index:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        _restrict_private(self.path)
        self.connection.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                memory_id UNINDEXED,
                tenant_id UNINDEXED,
                profile_id UNINDEXED,
                collection_id UNINDEXED,
                agent_id UNINDEXED,
                session_id UNINDEXED,
                content,
                kind,
                source_ids UNINDEXED,
                tokenize = 'porter unicode61'
            )"""
        )
        # Schema migration (FTS-2): tables created before the porter
        # tokenizer existed (pre-2026-08-05) use the default unicode61
        # tokenizer, which does NOT stem — "authentication" never matched
        # "authenticated", silently crippling recall on inflected queries.
        # Detect by reading the FTS5 config rank/tokenizer row and rebuild
        # the (derived, rebuildable) projection table if it lacks porter.
        self._ensure_porter_tokenizer()
        self.connection.commit()

    def _ensure_porter_tokenizer(self) -> None:
        """Recreate memory_fts with the porter tokenizer if it is missing.

        FTS5 persists its tokenizer choice in the table's CREATE
        statement (sqlite_master.sql). Tables created before the porter
        tokenizer existed (pre-2026-08-05) use the default unicode61
        tokenizer, which does NOT stem — "authentication" never matched
        "authenticated", silently crippling recall on inflected queries.
        The projection is derived and rebuildable (P0-1), so dropping
        the table is safe — the canonical store is the source of truth.
        """
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_fts'"
        ).fetchone()
        ddl = (row[0] if row else "") or ""
        if "porter" in ddl.casefold():
            return  # already correct
        # Recreate with the porter tokenizer. FTS5 cannot ALTER its
        # tokenizer; drop + recreate the virtual table. Any existing
        # projection content is dropped (rebuild from canonical).
        self.connection.execute("DROP TABLE IF EXISTS memory_fts")
        self.connection.execute(
            """CREATE VIRTUAL TABLE memory_fts USING fts5(
                memory_id UNINDEXED,
                tenant_id UNINDEXED,
                profile_id UNINDEXED,
                collection_id UNINDEXED,
                agent_id UNINDEXED,
                session_id UNINDEXED,
                content,
                kind,
                source_ids UNINDEXED,
                tokenize = 'porter unicode61'
            )"""
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def upsert(self, memory: Memory) -> None:
        self.connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory.id,))
        self.connection.execute(
            """INSERT INTO memory_fts
            (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, content, kind, source_ids)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory.id,
                memory.scope.tenant_id,
                memory.scope.profile_id,
                memory.scope.collection_id,
                memory.scope.agent_id,
                memory.scope.session_id,
                memory.content,
                memory.kind,
                " ".join(memory.source_observation_ids),
            ),
        )
        self.connection.commit()

    def delete(self, memory_id: str) -> None:
        self.connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        self.connection.commit()

    def count(self, scope: Scope) -> int:
        return int(
            self.connection.execute(
                """SELECT COUNT(*) FROM memory_fts
                   WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                     AND agent_id = ? AND session_id = ?""",
                tuple(scope.model_dump().values()),
            ).fetchone()[0]
        )

    def clear(self, scope: Scope | None = None) -> None:
        if scope is None:
            self.connection.execute("DELETE FROM memory_fts")
        else:
            self.connection.execute(
                """DELETE FROM memory_fts
                   WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                     AND agent_id = ? AND session_id = ?""",
                tuple(scope.model_dump().values()),
            )
        self.connection.commit()

    @staticmethod
    def _match_query(query: str) -> str:
        operator_tokens = {"and", "or", "not", "near"}
        tokens = [
            token.replace('"', '""')
            for token in re.findall(r"[\w]+", query, flags=re.UNICODE)
            if token.casefold() not in operator_tokens and token.casefold() not in _STOPWORDS
        ]
        # OR semantics: a natural-language query contains filler words
        # that never match, so AND (FTS5 default) returns nothing for
        # almost every real query. BM25 ranks rarer terms far above
        # common ones, so distinctive matches still surface first.
        return " OR ".join(f'"{token}"' for token in tokens)

    @classmethod
    def is_searchable(cls, query: str) -> bool:
        return bool(cls._match_query(query))

    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[FTSHit]:
        match = self._match_query(query)
        if not match:
            return []
        rows = self.connection.execute(
            """SELECT memory_id, bm25(memory_fts) AS rank,
                      snippet(memory_fts, 6, '[', ']', '...', 16)
               FROM memory_fts
               WHERE memory_fts MATCH ?
                 AND tenant_id = ? AND profile_id = ? AND collection_id = ?
                 AND agent_id = ? AND session_id = ?
               ORDER BY rank, content, memory_id LIMIT ?""",
            (match, *scope.model_dump().values(), limit),
        ).fetchall()
        return [FTSHit(memory_id=row[0], score=float(-row[1]), snippet=row[2]) for row in rows]

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        self.clear(scope)
        for memory in repository.list_memories(scope):
            self.upsert(memory)

    def digest(self, scope: Scope) -> str:
        """Stable digest over (memory_id, content) pairs in scope."""
        import hashlib

        rows = self.connection.execute(
            """SELECT memory_id, content FROM memory_fts
               WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                 AND agent_id = ? AND session_id = ?
               ORDER BY memory_id""",
            tuple(scope.model_dump().values()),
        ).fetchall()
        digest = hashlib.sha256()
        for memory_id, content in rows:
            digest.update(memory_id.encode())
            digest.update(b"\x00")
            digest.update(content.encode())
            digest.update(b"\x00")
        return digest.hexdigest()

    def health(self) -> str:
        """Adapter-neutral probe: returns ``ok`` or ``error``."""
        try:
            self.connection.execute("SELECT 1")
            return "ok"
        except Exception:  # pragma: no cover - defensive
            return "error"
