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

# 0.6.4: vectorised search is the DEFAULT. NumPy is a runtime dependency (pyproject), so every
# install, including the gateways' plugin environments, scores a scope as one float32 matrix
# instead of unpacking each vector in Python (~1.65 s per search at 31k vectors in 0.6.3, where
# the NumPy path was opt-in and NumPy was absent from the gateways). Results are byte-identical
# to the pure-Python path: NumPy only narrows the candidates, and every candidate's score is then
# recomputed with the same ``math.fsum`` contract and sorted with the same total key.
# ``SEVERIAN_OPT_NUMPY=0`` forces the pure-Python path (diagnostics); without NumPy it is used too.
_NUMPY_AVAILABLE = os.environ.get("SEVERIAN_OPT_NUMPY", "") != "0"
if _NUMPY_AVAILABLE:
    try:
        import numpy  # noqa: F401  (imported lazily again in _search_numpy)

        _NUMPY_AVAILABLE = True
    except ImportError:  # pragma: no cover - numpy is a declared dependency
        _NUMPY_AVAILABLE = False

# Unit round-off of IEEE single precision: bounds how far a NumPy float32 dot product can sit from
# the exactly-rounded ``math.fsum`` score (see ``_search_numpy``).
_FLOAT_EPS = 2.0**-24
_ID_CHUNK = 900  # bind parameters per IN (...) lookup, well under SQLite's limit


def vector_search_backend() -> str:
    """Which exact top-k path SQLite vector search uses in this process: ``numpy`` or ``python``."""
    return "numpy" if _NUMPY_AVAILABLE else "python"


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


def _exact_score(query_vector: list[float], vector: list[float]) -> float:
    """R0.2 score contract: ``math.fsum`` (Shewchuk-compensated), version-independent and
    bit-identical to the Rust kernel. Non-finite results raise ``VectorFormatError``."""
    try:
        score = math.fsum(left * right for left, right in zip(query_vector, vector, strict=True))
    except OverflowError as exc:
        raise VectorFormatError("non-finite dot product") from exc
    if not math.isfinite(score):
        raise VectorFormatError("non-finite dot product")
    return score


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
        # R2: optional native accel (auto|python|rust). Lazy import to
        # avoid the vector<->native_accel import cycle; adapter
        # construction is cheap and never raises for auto.
        from severian.infrastructure.native_accel import NativeAccelAdapter

        self._native_accel: NativeAccelAdapter | None = None
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

    def list_memory_ids(self, scope: Scope) -> set[str]:
        """Return exactly the memory IDs currently present in this scope.

        Benchmark evidence uses this to prove the scored vector projection,
        rather than relying on adapter ingestion bookkeeping.
        """
        rows = self.connection.execute(
            """SELECT memory_id FROM memory_vectors
               WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                 AND agent_id = ? AND session_id = ?""",
            tuple(scope.model_dump().values()),
        ).fetchall()
        return {str(row[0]) for row in rows}

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
        # zero-debt audit HIGH 5: query encoding is a SEPARATE contract
        # from document encoding. Embeddings that expose encode_query()
        # (e.g. instruction-prompted Qwen) get their query-side prompt
        # here; documents are ALWAYS encoded with the plain contract.
        encode_query = getattr(self.embedding, "encode_query", None)
        if encode_query is not None:
            query_vector = encode_query(query)
        else:
            query_vector = self.embedding.encode(query)
        if self.connection.execute(
            """SELECT 1 FROM memory_vectors
              WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                AND agent_id = ? AND session_id = ? LIMIT 1""",
            tuple(scope.model_dump().values()),
        ).fetchone() is None:
            return []
        if self._native_accel is None:
            from severian.infrastructure.native_accel import NativeAccelAdapter

            # Fail-closed (P0-A): the adapter owns auto fallback policy.
            # An explicit `rust` request with the optional module absent or
            # broken raises NativeAccelError HERE — it must never be caught
            # and silently replaced with a Python adapter, or explicit Rust
            # mode would degrade without the operator's knowledge.
            self._native_accel = NativeAccelAdapter()
        # R2: optional native exact top-k (auto|python|rust). The Python
        # reference remains the fallback and test oracle. Backend
        # identity is diagnostics-only — it never alters vectors/ranking.
        # Adapter is built lazily from SEVERIAN_VECTOR_BACKEND on first
        # search (auto -> rust when the wheel is installed, else python).
        if self._native_accel.selected == "rust":
            rows = self._scope_rows(scope)
            if not rows:
                return []
            native_rows = [(m, c, b) for m, b, c in rows]
            return self._native_accel.exact_top_k(query_vector, native_rows, limit)
        if (
            _NUMPY_AVAILABLE
            and len(query_vector) == self.embedding.dimension
            and all(math.isfinite(value) for value in query_vector)
        ):
            return self._search_numpy(scope, query_vector, limit=limit)
        return self._search_python(self._scope_rows(scope), query_vector, limit=limit)

    def _scope_rows(self, scope: Scope) -> list[Any]:
        return self.connection.execute(
            """SELECT memory_id, vector, content FROM memory_vectors
              WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                AND agent_id = ? AND session_id = ?""",
            tuple(scope.model_dump().values()),
        ).fetchall()

    def _search_python(self, rows: list[Any], query_vector: list[float], *, limit: int) -> list[VectorHit]:
        """The reference exact top-k: unpack, ``math.fsum`` score, sort by (score desc, content, id)."""
        # zero-debt 4.2: exact bounded top-k with a TOTAL tiebreak key
        # (score desc, content, memory_id — memory_id unique) so the
        # selection is deterministic and identical to a full sort.
        import heapq

        def _key(item: tuple[VectorHit, list[float], str]) -> tuple[float, str, str]:
            hit, _vector, content = item
            return (-hit.score, content, hit.memory_id)

        scored: list[tuple[VectorHit, list[float], str]] = []
        for memory_id, encoded, content in rows:
            vector = _unpack_vector(encoded, self.embedding.dimension)
            scored.append((VectorHit(memory_id=memory_id, score=_exact_score(query_vector, vector)), vector, content))
        if limit < len(scored):
            scored = heapq.nsmallest(limit, scored, key=_key)
        else:
            scored.sort(key=_key)
        return [hit for hit, _vector, _content in scored]

    def _search_numpy(self, scope: Scope, query_vector: list[float], *, limit: int) -> list[VectorHit]:
        """Vectorised exact top-k, byte-identical to ``_search_python`` (0.6.4).

        1. Read only ``(memory_id, vector)`` for the scope and score every row at once as a float32
           matrix product. Each approximate score is within ``tol`` of the exact ``math.fsum``
           score, where ``tol`` bounds float32 rounding (query cut, products, sums) for the
           largest ``||q|| * ||v||`` in the scope.
        2. Keep every row whose approximate score is >= (the limit-th best approximate score) -
           2 * tol. That provably contains every row of the exact top ``limit``, ties included.
        3. Recompute those candidates' scores with ``math.fsum`` and sort them with the reference
           key (score desc, content, memory_id), so scores and order equal the Python path's.

        A malformed or non-finite stored vector raises ``VectorFormatError`` exactly as the Python
        path does; anything unusual (a non-finite score) hands the whole scope to the Python path.
        """
        import numpy as np

        rows = self.connection.execute(
            """SELECT memory_id, vector FROM memory_vectors
              WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                AND agent_id = ? AND session_id = ?""",
            tuple(scope.model_dump().values()),
        ).fetchall()
        if not rows:
            return []
        dim = self.embedding.dimension
        width = dim * struct.calcsize("<f")
        for _memory_id, blob in rows:
            if len(blob) != width:
                _unpack_vector(blob, dim)  # raises the reference VectorFormatError
        n = len(rows)
        matrix32 = np.frombuffer(b"".join(row[1] for row in rows), dtype="<f4").reshape(n, dim)
        if not bool(np.isfinite(matrix32).all()):
            raise VectorFormatError("vector blob contains non-finite components")
        # Scores in float32 straight from the stored matrix: no float64 copy of the whole scope (a
        # 245 MB copy plus page faults cost seconds at 80k rows). The error bound below covers the
        # float32 query cut and float32 accumulation: |approx - exact| <= (dim + 2) * 2^-24 *
        # sum(|q_i * v_i|) <= (dim + 2) * 2^-24 * ||q|| * ||v|| (Cauchy-Schwarz), doubled for margin.
        with np.errstate(over="ignore", invalid="ignore"):
            query32 = np.asarray(query_vector, dtype=np.float32)
            scores = matrix32 @ query32
            row_norm = float(np.sqrt(np.max(np.einsum("ij,ij->i", matrix32, matrix32, dtype=np.float64))))
        query_norm = math.sqrt(math.fsum(value * value for value in query_vector))
        if not (bool(np.isfinite(scores).all()) and math.isfinite(row_norm) and math.isfinite(query_norm)):
            return self._search_python(self._scope_rows(scope), query_vector, limit=limit)
        tol = 2.0 * (dim + 4) * _FLOAT_EPS * query_norm * row_norm
        if limit < n:
            kth = float(np.partition(scores, n - limit)[n - limit])
            candidates = np.flatnonzero(scores >= kth - 2.0 * tol)
        else:
            candidates = np.arange(n)
        picked = [str(rows[int(i)][0]) for i in candidates]
        contents = self._contents(picked)
        scored: list[tuple[float, str, str]] = []
        for index, memory_id in zip(candidates, picked, strict=True):
            vector = [float(value) for value in matrix32[int(index)]]
            scored.append((-_exact_score(query_vector, vector), contents[memory_id], memory_id))
        scored.sort()
        return [VectorHit(memory_id=memory_id, score=-negative) for negative, _content, memory_id in scored[:limit]]

    def _contents(self, memory_ids: list[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for start in range(0, len(memory_ids), _ID_CHUNK):
            chunk = memory_ids[start : start + _ID_CHUNK]
            marks = ",".join("?" for _ in chunk)
            for memory_id, content in self.connection.execute(
                f"SELECT memory_id, content FROM memory_vectors WHERE memory_id IN ({marks})",  # noqa: S608 - placeholders
                chunk,
            ):
                found[str(memory_id)] = str(content)
        return found

    def scope_contents(self, scope: Scope) -> dict[str, str]:
        """``memory_id -> content`` of every vector row in this scope (no vectors are read)."""
        rows = self.connection.execute(
            """SELECT memory_id, content FROM memory_vectors
               WHERE tenant_id = ? AND profile_id = ? AND collection_id = ?
                 AND agent_id = ? AND session_id = ?""",
            tuple(scope.model_dump().values()),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def write_encoded(self, rows: list[tuple[Memory, list[float]]], delete_ids: list[str]) -> None:
        """Upsert already-encoded rows and delete ``delete_ids`` in ONE transaction (0.6.4).

        Nothing is encoded here, so nothing can fail half-way on the embedding: either every
        change lands or none does.
        """
        packed = [
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
            for memory, vector in rows
        ]
        if not packed and not delete_ids:
            return
        try:
            if delete_ids:
                self.connection.executemany(
                    "DELETE FROM memory_vectors WHERE memory_id = ?", [(memory_id,) for memory_id in delete_ids]
                )
            if packed:
                self.connection.executemany(
                    """INSERT INTO memory_vectors
                    (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, content, vector)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        tenant_id=excluded.tenant_id, profile_id=excluded.profile_id,
                        collection_id=excluded.collection_id, agent_id=excluded.agent_id,
                        session_id=excluded.session_id, content=excluded.content, vector=excluded.vector""",
                    packed,
                )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        """Make this scope's rows equal its active memories, failure-safe (0.6.4).

        0.6.3 cleared the scope (own commit) and then encoded every memory; one text the embedding
        refused, or a sidecar timeout, left the scope EMPTY (20/11/2026). Now every vector that is
        still right is kept, only missing or changed memories are encoded, and all encoding happens
        BEFORE the single write transaction: an encode failure raises with the scope untouched.
        """
        memories = list(repository.list_memories(scope))
        existing = self.scope_contents(scope)
        active = {memory.id for memory in memories}
        encoded = [
            (memory, self.embedding.encode(memory.content))
            for memory in memories
            if existing.get(memory.id) != memory.content
        ]
        stale = sorted(memory_id for memory_id in existing if memory_id not in active)
        self.write_encoded(encoded, stale)

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

    def backend_diagnostics(self) -> dict[str, object]:
        """R2.3 visible backend diagnostics (never part of the vector
        fingerprint — acceleration must not alter vectors or ranking)."""
        if self._native_accel is None:
            return {
                "requested": "auto",
                "selected": "python",
                "native_version": None,
                "fallback_reason": "not initialised (no search yet)",
                "calls": 0,
                "failures": 0,
            }
        d = self._native_accel.diagnostics
        return {
            "requested": d.requested,
            "selected": d.selected,
            "native_version": d.native_version,
            "fallback_reason": d.fallback_reason,
            "calls": d.calls,
            "failures": d.failures,
        }
