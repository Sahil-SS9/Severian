from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
from pathlib import Path
from typing import Any, Protocol

from severian.application.ports import CanonicalRepository
from severian.domain.errors import VectorFormatError
from severian.domain.hits import VectorHit  # re-export for backward compat (ARCH-03)
from severian.domain.models import Memory, Scope
from severian.domain.text import STOPWORDS, porter_stem
from severian.infrastructure.embedding_metadata import (
    EmbeddingMetadata,
    EmbeddingSpaceMismatchError,
    metadata_from_dict,
)
from severian.infrastructure.embedding_registry import resolve_embedding_metadata

_STOPWORDS: frozenset[str] = STOPWORDS  # backward-compat alias (ARCH-03)

# zero-debt 4.2: the ONLY supported vector storage format is packed
# little-endian float32 BLOB. Legacy JSON-text rows are never mutated
# in place — a legacy index is refused at open with rebuild guidance.
VECTOR_STORAGE_FORMAT = "blob_f32le"
_FORMAT_KEY = "vector_storage_format"

# Optional NumPy acceleration, opt-in ONLY (SEVERIAN_OPT_NUMPY=1): the
# hash-only installation must never carry a compulsory heavy dependency
# for a microbenchmark win. When off (default), search uses the pure
# Python heapq path with identical ordering semantics.
_NUMPY_AVAILABLE = os.environ.get("SEVERIAN_OPT_NUMPY", "") == "1"
if _NUMPY_AVAILABLE:
    try:
        import numpy  # noqa: F401  (imported lazily again in _search_numpy)

        _NUMPY_AVAILABLE = True
    except ImportError:
        _NUMPY_AVAILABLE = False


def _pack_vector(vector: list[float]) -> bytes:
    """Pack floats as little-endian float32 BLOB (zero-debt 4.1).

    Rejects NaN/Inf: a non-finite component silently corrupts every
    downstream distance comparison, so it must fail at write time.
    """
    for component in vector:
        if not math.isfinite(component):
            raise ValueError("embedding produced a non-finite component")
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack_vector(blob: bytes, dimension: int) -> list[float]:
    """Unpack a little-endian float32 BLOB, validating its dimension.

    Malformed length or a non-finite payload raises VectorFormatError:
    the projection is disposable — rebuild, never trust the file.
    """
    expected = dimension * struct.calcsize("<f")
    if len(blob) != expected:
        raise VectorFormatError(
            f"malformed vector blob: {len(blob)} bytes, expected {expected} "
            f"for dimension {dimension}"
        )
    values = list(struct.unpack(f"<{dimension}f", blob))
    if any(not math.isfinite(value) for value in values):
        raise VectorFormatError("vector blob contains non-finite components")
    return values


def _meta_json(metadata: EmbeddingMetadata) -> str:
    return json.dumps(metadata.to_dict(), sort_keys=True)


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


class EmbeddingProvider(Protocol):
    """Pluggable semantic embedding provider (A-1).

    Any implementation exposing ``dimension`` and ``encode`` satisfies
    the protocol: the deterministic :class:`HashEmbedding` (default) or
    a neural embedding such as Granite R2 (97M, Apache-2.0, CPU).
    """

    dimension: int

    def encode(self, text: str) -> list[float]: ...


class HashEmbedding:
    def __init__(self, dimension: int = 256, *, skip_stopwords: bool = True, stem: bool = True):
        if dimension < 8:
            raise ValueError("embedding dimension must be at least 8")
        self.dimension = dimension
        self.skip_stopwords = skip_stopwords
        # Stemming aligns the hash tokenizer with the FTS5 porter
        # tokenizer: "authentication"/"authenticated"/"authenticate" all
        # hash identically, so vector and lexical projections agree on
        # inflected queries (fixes the fused-rank gap on paraphrase
        # probes). Deterministic; on by default.
        self.stem = stem

    def encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        tokens = re.findall(r"[\w]+", text.casefold(), flags=re.UNICODE)
        for token in tokens:
            if self.skip_stopwords and token in _STOPWORDS:
                continue
            if self.stem:
                token = porter_stem(token)
            digest = hashlib.sha256(token.encode()).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class LocalVectorIndex:
    def __init__(self, path: str | Path, embedding: EmbeddingProvider):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.embedding = embedding
        self.connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=30000")
        _restrict_private(self.path)
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS vector_space_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )"""
        )
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS memory_vectors (
                memory_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                profile_id TEXT NOT NULL,
                collection_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                content TEXT NOT NULL,
                vector TEXT NOT NULL
            )"""
        )
        # zero-debt 4.2: composite scope index — search filters on all
        # five scope dimensions; without it every query is a full scan.
        self.connection.execute(
            """CREATE INDEX IF NOT EXISTS idx_memory_vectors_scope
            ON memory_vectors (tenant_id, profile_id, collection_id, agent_id, session_id)"""
        )
        self._metadata = resolve_embedding_metadata(embedding)
        self._stamp_or_verify_space()
        self.connection.commit()

    def _stamp_or_verify_space(self) -> None:
        """Stamp an empty index with the active identity; verify a non-empty one.

        Non-empty index + different fingerprint → EmbeddingSpaceMismatchError
        with rebuild guidance. An index that has rows but NO stored identity
        (pre-identity store) is treated as unknown space and must be rebuilt
        explicitly — opening it for writes would silently mix spaces.

        zero-debt 4.1: the storage format is verified/stamped in the SAME
        meta table. A legacy JSON-format index (or an unversioned one with
        rows) is REFUSED — never mutated in place; rebuild guidance is the
        only path (the projection is disposable).
        """
        row = self.connection.execute(
            "SELECT value FROM vector_space_meta WHERE key = 'embedding_metadata'"
        ).fetchone()
        count = int(self.connection.execute(
            "SELECT COUNT(*) FROM memory_vectors"
        ).fetchone()[0])

        format_row = self.connection.execute(
            "SELECT value FROM vector_space_meta WHERE key = ?", (_FORMAT_KEY,)
        ).fetchone()
        if format_row is None:
            if count > 0:
                raise VectorFormatError(
                    f"vector index at {self.path} uses an unversioned/legacy "
                    f"JSON storage format; rebuild it from canonical memories "
                    f"(severian rebuild-vectors) — the projection is "
                    f"disposable and is never migrated in place"
                )
            self.connection.execute(
                "INSERT INTO vector_space_meta (key, value) VALUES (?, ?)",
                (_FORMAT_KEY, VECTOR_STORAGE_FORMAT),
            )
        elif format_row[0] != VECTOR_STORAGE_FORMAT:
            raise VectorFormatError(
                f"vector index at {self.path} uses storage format "
                f"'{format_row[0]}' but this build only supports "
                f"'{VECTOR_STORAGE_FORMAT}'; rebuild it from canonical "
                f"memories (severian rebuild-vectors)"
            )

        if row is None:
            if count > 0:
                raise EmbeddingSpaceMismatchError(
                    stored=EmbeddingMetadata(
                        provider="unknown", model_id="unknown", revision="unknown",
                        dimension=0, normalize=False, pooling="unknown",
                        prompt_contract="", runtime="unknown"),
                    active=self._metadata,
                    path_or_dsn=str(self.path),
                )
            self.connection.execute(
                "INSERT INTO vector_space_meta (key, value) VALUES ('embedding_metadata', ?)",
                (_meta_json(self._metadata),),
            )
            return
        stored = metadata_from_dict(json.loads(row[0]))
        if stored.fingerprint != self._metadata.fingerprint:
            if count == 0:
                # Empty index: no data at risk — restamp with the active
                # space so a new embedding can initialise it.
                self.connection.execute(
                    "UPDATE vector_space_meta SET value = ? WHERE key = 'embedding_metadata'",
                    (_meta_json(self._metadata),),
                )
                return
            raise EmbeddingSpaceMismatchError(
                stored=stored, active=self._metadata, path_or_dsn=str(self.path)
            )

    def close(self) -> None:
        self.connection.close()

    def upsert(self, memory: Memory) -> None:
        vector = self.embedding.encode(memory.content)
        self.connection.execute(
            """INSERT INTO memory_vectors
            (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, content, vector)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id) DO UPDATE SET
                tenant_id=excluded.tenant_id, profile_id=excluded.profile_id,
                collection_id=excluded.collection_id, agent_id=excluded.agent_id,
                session_id=excluded.session_id, content=excluded.content, vector=excluded.vector""",
            (
                memory.id,
                memory.scope.tenant_id,
                memory.scope.profile_id,
                memory.scope.collection_id,
                memory.scope.agent_id,
                memory.scope.session_id,
                memory.content,
                _pack_vector(vector),
            ),
        )
        self.connection.commit()

    def upsert_many(self, memories: list[Memory]) -> None:
        """Bulk upsert in ONE transaction (zero-debt 3.3).

        The rebuild/batch path must not commit per row — one commit for
        the whole batch. Atomicity: any failure rolls back the batch.
        """
        if not memories:
            return
        rows = []
        for memory in memories:
            vector = self.embedding.encode(memory.content)
            rows.append(
                (
                    memory.id,
                    memory.scope.tenant_id,
                    memory.scope.profile_id,
                    memory.scope.collection_id,
                    memory.scope.agent_id,
                    memory.scope.session_id,
                    memory.content,
                    _pack_vector(vector),
                )
            )
        try:
            self.connection.executemany(
                """INSERT INTO memory_vectors
                (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, content, vector)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id, profile_id=excluded.profile_id,
                    collection_id=excluded.collection_id, agent_id=excluded.agent_id,
                    session_id=excluded.session_id, content=excluded.content, vector=excluded.vector""",
                rows,
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def delete(self, memory_id: str) -> None:
        self.connection.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (memory_id,))
        self.connection.commit()

    def count(self, scope: Scope) -> int:
        return int(
            self.connection.execute(
                """SELECT COUNT(*) FROM memory_vectors
                   WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                     AND agent_id = ? AND session_id = ?""",
                tuple(scope.model_dump().values()),
            ).fetchone()[0]
        )

    def clear(self, scope: Scope | None = None) -> None:
        if scope is None:
            self.connection.execute("DELETE FROM memory_vectors")
        else:
            self.connection.execute(
                """DELETE FROM memory_vectors
                   WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                     AND agent_id = ? AND session_id = ?""",
                tuple(scope.model_dump().values()),
            )
        self.connection.commit()

    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[VectorHit]:
        query_vector = self.embedding.encode(query)
        rows = self.connection.execute(
            """SELECT memory_id, vector, content FROM memory_vectors
              WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                AND agent_id = ? AND session_id = ?""",
            tuple(scope.model_dump().values()),
        ).fetchall()
        if not rows:
            return []
        # zero-debt 4.2: exact bounded top-k with a TOTAL tiebreak key
        # (score desc, content, memory_id — memory_id unique) so the
        # selection is deterministic and identical to a full sort.
        # Optional NumPy acceleration (opt-in env) with the SAME key via
        # lexsort; pure-Python heapq fallback needs no numpy at all.
        if _NUMPY_AVAILABLE:
            try:
                return self._search_numpy(rows, query_vector, limit=limit)
            except Exception:  # noqa: BLE001 - fall back to pure python
                pass
        import heapq

        def _key(item: tuple[VectorHit, list[float], str]) -> tuple[float, str, str]:
            hit, _vector, content = item
            return (-hit.score, content, hit.memory_id)

        scored: list[tuple[VectorHit, list[float], str]] = []
        for memory_id, encoded, content in rows:
            vector = _unpack_vector(encoded, self.embedding.dimension)
            score = sum(left * right for left, right in zip(query_vector, vector, strict=True))
            scored.append((VectorHit(memory_id=memory_id, score=score), vector, content))
        if limit < len(scored):
            scored = heapq.nsmallest(limit, scored, key=_key)
        else:
            scored.sort(key=_key)
        return [hit for hit, _vector, _content in scored]

    def _search_numpy(
        self,
        rows: list[Any],
        query_vector: list[float],
        *,
        limit: int,
    ) -> list[VectorHit]:
        """Vectorised exact top-k (opt-in; SEVERIAN_OPT_NUMPY=1).

        Decodes the whole scope's BLOBs as one float32 matrix, computes
        scores with float64 matmul, and selects with np.lexsort over the
        SAME total key (score desc, content, memory_id). lexsort is the
        exact analogue of the pure-Python sort, so ordering is
        deterministic and identical — no argpartition (its arbitrary
        tiebreak would change semantics at the k boundary).
        """
        import numpy as np

        n = len(rows)
        memory_ids: list[str] = []
        contents: list[str] = []
        blobs = bytearray()
        for memory_id, encoded, content in rows:
            memory_ids.append(memory_id)
            contents.append(content)
            blobs.extend(encoded)
        dim = self.embedding.dimension
        matrix = np.frombuffer(bytes(blobs), dtype="<f4").reshape(n, dim).astype(np.float64)
        scores = matrix @ np.asarray(query_vector, dtype=np.float64)
        # lexsort: LAST key is primary. (-scores, contents, memory_ids)
        # == (score desc, content, memory_id) total order, exactly.
        order = np.lexsort(
            (
                np.asarray(memory_ids),
                np.asarray(contents),
                -scores,
            )
        )
        return [
            VectorHit(memory_id=memory_ids[i], score=float(scores[i]))
            for i in order[:limit]
        ]

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        self.clear(scope)
        self.upsert_many(list(repository.list_memories(scope)))

    def digest(self, scope: Scope) -> str:
        """Stable digest over (memory_id, content) pairs in scope."""
        import hashlib

        rows = self.connection.execute(
            """SELECT memory_id, content FROM memory_vectors
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
