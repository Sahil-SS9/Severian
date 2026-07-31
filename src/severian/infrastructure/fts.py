from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Scope


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
                source_ids UNINDEXED
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
            if token.casefold() not in operator_tokens
        ]
        return " AND ".join(f'"{token}"' for token in tokens)

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
               ORDER BY rank LIMIT ?""",
            (match, *scope.model_dump().values(), limit),
        ).fetchall()
        return [FTSHit(memory_id=row[0], score=float(-row[1]), snippet=row[2]) for row in rows]

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        self.clear(scope)
        for memory in repository.list_memories(scope):
            self.upsert(memory)
