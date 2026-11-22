from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from severian.application.message_units import MESSAGE_UNIT_KIND, message_units_enabled
from severian.application.ports import CanonicalRepository
from severian.domain.hits import FTSHit  # re-export for backward compat (ARCH-03)
from severian.domain.models import Memory, Scope
from severian.infrastructure.vector import _STOPWORDS

_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)
_SCOPE_WHERE = "tenant_id = ? AND profile_id = ? AND collection_id = ? AND agent_id = ? AND session_id = ?"
_FTS_COLUMNS = "memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, content, kind, source_ids"
# 0.6.4: S1 message units live in their own FTS5 table. BM25 statistics (row count, average length,
# term frequencies) are per table, so units in ``memory_fts`` changed every other memory's lexical
# score even with SEVERIAN_MESSAGE_UNITS off. With units apart, switch-off search reads exactly the
# rows (and statistics) it read before any unit existed.
_UNIT_TABLE = "unit_fts"
_UNITS_SPLIT_KEY = "units_in_unit_fts"


def _table_for(kind: str) -> str:
    return _UNIT_TABLE if kind == MESSAGE_UNIT_KIND else "memory_fts"


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
        # Step 4 cues: when a memory will be needed (likely questions, situations). A separate
        # one-row-per-memory table, so the memory's own text, its digest and its vector never change.
        self.connection.execute(
            """CREATE VIRTUAL TABLE IF NOT EXISTS cue_fts USING fts5(
                memory_id UNINDEXED,
                tenant_id UNINDEXED,
                profile_id UNINDEXED,
                collection_id UNINDEXED,
                agent_id UNINDEXED,
                session_id UNINDEXED,
                cue,
                tokenize = 'porter unicode61'
            )"""
        )
        self.connection.execute(
            f"""CREATE VIRTUAL TABLE IF NOT EXISTS {_UNIT_TABLE} USING fts5(
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
        self.connection.execute("CREATE TABLE IF NOT EXISTS fts_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self._split_units()
        self.connection.commit()

    def _split_units(self) -> None:
        """Move message units indexed by 0.6.3 (in ``memory_fts``) into ``unit_fts``, once per file."""
        if self.connection.execute("SELECT 1 FROM fts_meta WHERE key = ?", (_UNITS_SPLIT_KEY,)).fetchone():
            return
        self.connection.execute(
            f"INSERT INTO {_UNIT_TABLE} ({_FTS_COLUMNS}) SELECT {_FTS_COLUMNS} FROM memory_fts WHERE kind = ?",
            (MESSAGE_UNIT_KIND,),
        )
        self.connection.execute("DELETE FROM memory_fts WHERE kind = ?", (MESSAGE_UNIT_KIND,))
        self.connection.execute("INSERT INTO fts_meta (key, value) VALUES (?, '1')", (_UNITS_SPLIT_KEY,))

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
        self._write_row(memory)
        self._write_cues(memory)
        self.connection.commit()

    def _write_row(self, memory: Memory) -> None:
        """Replace the memory's lexical row in the table its kind belongs to; the caller commits."""
        self.connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory.id,))
        self.connection.execute(f"DELETE FROM {_UNIT_TABLE} WHERE memory_id = ?", (memory.id,))
        self.connection.execute(
            f"""INSERT INTO {_table_for(memory.kind)} ({_FTS_COLUMNS})
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

    def _write_cues(self, memory: Memory) -> None:
        """Replace the memory's cue row (none when it has no cues); the caller commits."""
        self.connection.execute("DELETE FROM cue_fts WHERE memory_id = ?", (memory.id,))
        if memory.cues:
            self.connection.execute(
                """INSERT INTO cue_fts (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, cue)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (memory.id, *memory.scope.model_dump().values(), "\n".join(memory.cues)),
            )

    def upsert_many(self, memories: list[Memory]) -> None:
        """Bulk upsert in ONE transaction (zero-debt 3.3).

        The rebuild/batch path must not commit per row — one commit for
        the whole batch. Atomicity: any failure rolls back the batch.
        """
        if not memories:
            return
        try:
            for memory in memories:
                self._write_row(memory)
                self._write_cues(memory)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def delete(self, memory_id: str) -> None:
        for table in ("memory_fts", _UNIT_TABLE, "cue_fts"):
            self.connection.execute(f"DELETE FROM {table} WHERE memory_id = ?", (memory_id,))  # noqa: S608
        self.connection.commit()

    def count(self, scope: Scope) -> int:
        return sum(
            int(
                self.connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {_SCOPE_WHERE}",  # noqa: S608 - fixed names
                    tuple(scope.model_dump().values()),
                ).fetchone()[0]
            )
            for table in ("memory_fts", _UNIT_TABLE)
        )

    def _delete_scope(self, scope: Scope | None) -> None:
        """Delete every lexical, unit and cue row (of ``scope``, or all); the caller commits."""
        for table in ("memory_fts", _UNIT_TABLE, "cue_fts"):
            if scope is None:
                self.connection.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
            else:
                self.connection.execute(
                    f"DELETE FROM {table} WHERE {_SCOPE_WHERE}",  # noqa: S608 - fixed names
                    tuple(scope.model_dump().values()),
                )

    def clear(self, scope: Scope | None = None) -> None:
        self._delete_scope(scope)
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
        rows = self._search_table("memory_fts", match, scope, limit)
        if message_units_enabled():
            # Switch on: units compete for the same slots, merged on the same key the SQL orders by.
            units = self._search_table(_UNIT_TABLE, match, scope, limit)
            rows = sorted([*rows, *units], key=lambda row: (row[1], row[3], row[0]))[:limit]
        # Switch off (0.6.4): only memory_fts, whose rows and BM25 statistics hold no unit, so the
        # result is byte-identical to the same store before any unit was added.
        return [FTSHit(memory_id=row[0], score=float(-row[1]), snippet=row[2]) for row in rows]

    def _search_table(self, table: str, match: str, scope: Scope, limit: int) -> list[tuple[str, float, str, str]]:
        rows = self.connection.execute(
            f"""SELECT memory_id, bm25({table}) AS rank,
                      snippet({table}, 6, '[', ']', '...', 16), content
               FROM {table}
               WHERE {table} MATCH ?
                 AND {_SCOPE_WHERE}
               ORDER BY rank, content, memory_id LIMIT ?""",  # noqa: S608 - fixed names
            (match, *scope.model_dump().values(), limit),
        ).fetchall()
        return [(str(row[0]), float(row[1]), str(row[2]), str(row[3])) for row in rows]

    def search_cues(self, scope: Scope, query: str, *, limit: int = 20) -> list[FTSHit]:
        """Memories whose cues match ``query`` (best first). Empty when no memory has cues."""
        match = self._match_query(query)
        if not match:
            return []
        rows = self.connection.execute(
            """SELECT memory_id, bm25(cue_fts) AS rank
               FROM cue_fts
               WHERE cue_fts MATCH ?
                 AND tenant_id = ? AND profile_id = ? AND collection_id = ?
                 AND agent_id = ? AND session_id = ?
               ORDER BY rank, memory_id LIMIT ?""",
            (match, *scope.model_dump().values(), limit),
        ).fetchall()
        return [FTSHit(memory_id=row[0], score=float(-row[1]), snippet="") for row in rows]

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        """Replace the scope's lexical, unit and cue rows from its active memories in ONE transaction."""
        memories = list(repository.list_memories(scope))
        try:
            self._delete_scope(scope)
            for memory in memories:
                self._write_row(memory)
                self._write_cues(memory)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def digest(self, scope: Scope) -> str:
        """Stable digest over (memory_id, content) pairs in scope (memories and units together)."""
        import hashlib

        rows: list[tuple[str, str]] = []
        for table in ("memory_fts", _UNIT_TABLE):
            rows.extend(
                (str(row[0]), str(row[1]))
                for row in self.connection.execute(
                    f"SELECT memory_id, content FROM {table} WHERE {_SCOPE_WHERE}",  # noqa: S608 - fixed names
                    tuple(scope.model_dump().values()),
                )
            )
        rows.sort()
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
