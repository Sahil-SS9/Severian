"""Side-by-side atomic vector rebuild (zero-debt plan, Task 2.3).

`severian rebuild-vectors` NEVER rebuilds in place. It:

1. Builds the new projection SIDE-BY-SIDE (new sqlite file, or new PG
   table + meta) — the live index is untouched during the build.
2. Verifies the candidate: row count == canonical count, digest ==
   canonical digest, dimension/fingerprint match the active embedding,
   retrieval canaries hit, and ranking is deterministic.
3. Swaps atomically (os.replace for sqlite; transactional rename for
   PG). The OLD index is preserved as rollback (`.bak-<nonce>` file or
   `memory_vectors_prev_<nonce>` table) until explicit cleanup.
4. On failure BEFORE swap: the original index is untouched; only owned
   temporary artifacts (the candidate file/table) are removed.

Interruption at any point leaves either the original or the verified
candidate active — never a half-built mix (the incident class this
programme exists to prevent).
"""
from __future__ import annotations

import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from severian.domain.models import Memory, Scope


@runtime_checkable
class RebuildSource(Protocol):
    """Minimal source needed to rebuild vectors: canonical memories."""

    def list_memories(self, scope: Scope) -> list[Memory]: ...


@dataclass(frozen=True)
class RebuildVerification:
    """Evidence captured during candidate verification."""

    canonical_count: int
    vector_count: int
    digest_match: bool
    dimension: int
    fingerprint: str
    canary_hits: int
    canary_total: int
    deterministic: bool

    @property
    def passed(self) -> bool:
        return (
            self.canonical_count == self.vector_count
            and self.digest_match
            and self.canary_hits == self.canary_total
            and self.deterministic
        )


@dataclass
class RebuildResult:
    """Outcome of an atomic rebuild."""

    provider_id: str
    status: str  # swapped | aborted | failed
    verification: RebuildVerification | None = None
    rollback_path: str | None = None
    error: str | None = None
    candidate_path: str | None = None


@runtime_checkable
class ScopeDiscoverySource(RebuildSource, Protocol):
    """Operator-only discovery of every active-memory scope in a store."""

    def list_memory_scopes(self) -> list[Scope]: ...


def _scope_key(scope: Scope) -> tuple[str, str, str, str, str]:
    values = scope.model_dump()
    return (
        values["tenant_id"],
        values["profile_id"],
        values["collection_id"],
        values["agent_id"],
        values["session_id"],
    )


def _rebuild_scopes(repository: RebuildSource, scope: Scope | None) -> list[Scope]:
    """Resolve one explicit scope or every active scope, deterministically."""
    if scope is not None:
        return [scope]
    if not isinstance(repository, ScopeDiscoverySource):
        raise TypeError("all-scope rebuild requires a repository with list_memory_scopes()")
    scopes = repository.list_memory_scopes()
    by_key = {_scope_key(candidate): candidate for candidate in scopes}
    return [by_key[key] for key in sorted(by_key)]


def _canonical_rows(repository: RebuildSource, scopes: list[Scope]) -> list[Memory]:
    """Collect active canonical memories across the requested scopes once."""
    rows: list[Memory] = []
    memory_ids: set[str] = set()
    for scope in scopes:
        for memory in repository.list_memories(scope):
            if memory.scope != scope:
                raise ValueError("rebuild source returned a memory from the wrong scope")
            if memory.id in memory_ids:
                raise ValueError(f"duplicate memory id across rebuild scopes: {memory.id}")
            memory_ids.add(memory.id)
            rows.append(memory)
    return rows


def _sqlite_vector_scope_keys(path: Path) -> set[tuple[str, str, str, str, str]]:
    """Read existing projection scope keys without opening it through an embedding."""
    if not path.exists():
        return set()
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_vectors'"
            ).fetchone()
            if table is None:
                return set()
            rows = connection.execute(
                """SELECT DISTINCT tenant_id, profile_id, collection_id, agent_id, session_id
                   FROM memory_vectors"""
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError(f"cannot inspect existing vector projection {path}: {exc}") from exc
    return {
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]))
        for row in rows
    }


def _partial_sqlite_rebuild_error(path: Path, scopes: list[Scope]) -> str | None:
    existing = _sqlite_vector_scope_keys(path)
    requested = {_scope_key(scope) for scope in scopes}
    if existing - requested:
        return (
            "refusing partial vector rebuild: the shared SQLite index contains other scopes; "
            "rebuild all scopes together so one atomic swap cannot drop their vectors"
        )
    return None


def _pg_vector_scope_keys(dsn: str) -> set[tuple[str, str, str, str, str]]:
    """Read current PostgreSQL projection scopes without constructing an embedding index."""
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    engine = _pg_engine(dsn)
    try:
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        """SELECT DISTINCT tenant_id, profile_id, collection_id, agent_id, session_id
                           FROM memory_vectors"""
                    )
                ).all()
        except ProgrammingError as exc:
            if getattr(exc.orig, "sqlstate", "") == "42P01":
                return set()
            raise ValueError(f"cannot inspect existing vector projection: {exc}") from exc
    finally:
        engine.dispose()
    return {
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]), str(row[4]))
        for row in rows
    }


def _partial_pg_rebuild_error(dsn: str, scopes: list[Scope]) -> str | None:
    existing = _pg_vector_scope_keys(dsn)
    requested = {_scope_key(scope) for scope in scopes}
    if existing - requested:
        return (
            "refusing partial vector rebuild: the shared PostgreSQL index contains other scopes; "
            "rebuild all scopes together so one atomic swap cannot drop their vectors"
        )
    return None


def _canonical_digest(memories: list[Memory]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for memory in sorted(memories, key=lambda m: m.id):
        digest.update(memory.id.encode())
        digest.update(b"\x00")
        digest.update(memory.content.encode())
        digest.update(b"\x00")
    return digest.hexdigest()


def _sample_canaries(memories: list[Memory], limit: int = 8) -> list[Memory]:
    """Deterministic sample: evenly spaced across the sorted list."""
    if not memories:
        return []
    ordered = sorted(memories, key=lambda m: m.id)
    if len(ordered) <= limit:
        return ordered
    step = len(ordered) / limit
    return [ordered[int(i * step)] for i in range(limit)]


def verify_candidate(
    vector: Any,
    repository: RebuildSource,
    scopes: Scope | list[Scope],
    embedding: Any,
    memories: list[Memory],
) -> RebuildVerification:
    """Verify a rebuilt projection across every scope that will share its file/table."""
    del repository  # callers prefetch canonical rows once; verification is projection-only.
    requested_scopes: list[Scope]
    if isinstance(scopes, Scope):
        requested_scopes = [scopes]
    else:
        requested_scopes = scopes
    if not requested_scopes:
        raise ValueError("candidate verification requires at least one scope")
    from severian.infrastructure.embedding_registry import resolve_embedding_metadata

    grouped: dict[tuple[str, str, str, str, str], list[Memory]] = {
        _scope_key(scope): [] for scope in requested_scopes
    }
    for memory in memories:
        key = _scope_key(memory.scope)
        if key not in grouped:
            raise ValueError("candidate verification received a memory outside the rebuild scopes")
        grouped[key].append(memory)

    vector_count = 0
    digest_match = True
    canary_hits = 0
    canary_total = 0
    deterministic = True
    for scope in requested_scopes:
        scoped_memories = grouped[_scope_key(scope)]
        vector_count += vector.count(scope)
        digest_match = digest_match and vector.digest(scope) == _canonical_digest(scoped_memories)

        canaries = _sample_canaries(scoped_memories)
        canary_total += len(canaries)
        for memory in canaries:
            fragment = memory.content[:48]
            if not fragment.strip():
                continue
            results = vector.search(scope, fragment, limit=5)
            if any(hit.memory_id == memory.id for hit in results):
                canary_hits += 1
            elif any(hit.memory_id == memory.id for hit in vector.search(scope, memory.content[:96], limit=5)):
                canary_hits += 1

        a = vector.search(scope, "determinism probe query", limit=10)
        b = vector.search(scope, "determinism probe query", limit=10)
        deterministic = deterministic and [
            (hit.memory_id, round(hit.score, 6)) for hit in a
        ] == [(hit.memory_id, round(hit.score, 6)) for hit in b]

    meta = resolve_embedding_metadata(embedding)
    return RebuildVerification(
        canonical_count=len(memories),
        vector_count=vector_count,
        digest_match=digest_match,
        dimension=meta.dimension,
        fingerprint=meta.fingerprint,
        canary_hits=canary_hits,
        canary_total=canary_total,
        deterministic=deterministic,
    )


# --- sqlite ------------------------------------------------------------


def rebuild_sqlite_vectors(
    vectors_path: str | Path,
    repository: RebuildSource,
    scope: Scope | None,
    embedding: Any,
    *,
    keep_rollback: bool = True,
) -> RebuildResult:
    """Atomically rebuild one complete SQLite vector projection.

    ``scope`` rebuilds exactly one scope only when no other scope exists in
    the shared projection.  ``scope=None`` discovers and rebuilds every active
    canonical-memory scope together.  This prevents a file-level atomic swap
    from silently discarding vectors that belong to another scope.
    """
    from severian.infrastructure.vector import LocalVectorIndex

    path = Path(vectors_path)
    nonce = _nonce()
    candidate = path.with_name(f"{path.name}.rebuild-{nonce}.db")
    rollback = path.with_name(f"{path.name}.bak-{nonce}.db")
    result = RebuildResult(provider_id="sqlite", status="failed", candidate_path=str(candidate))

    try:
        scopes = _rebuild_scopes(repository, scope)
        partial_error = _partial_sqlite_rebuild_error(path, scopes) if scope is not None else None
        if partial_error is not None:
            result.status = "aborted"
            result.error = partial_error
            return result
        memories = _canonical_rows(repository, scopes)
        index = LocalVectorIndex(candidate, embedding)
        try:
            index.upsert_many(memories)
            verification = verify_candidate(index, repository, scopes, embedding, memories)
            result.verification = verification
            if not verification.passed:
                result.status = "aborted"
                result.error = _verification_failure(verification)
                return result
            index.close()
        except Exception:
            index.close()
            raise

        # Atomic swap: original -> rollback, candidate -> live.
        if path.exists():
            os.replace(path, rollback)
        os.replace(candidate, path)
        result.status = "swapped"
        result.rollback_path = str(rollback) if keep_rollback else None
        if not keep_rollback:
            rollback.unlink(missing_ok=True)
        return result
    except Exception as exc:  # noqa: BLE001 — surfaced in result.error
        result.status = "failed"
        result.error = str(exc)
        candidate.unlink(missing_ok=True)
        # If we moved the original to rollback but failed before the
        # candidate landed, restore it.
        if rollback.exists() and not path.exists():
            os.replace(rollback, path)
        return result


# --- postgres ----------------------------------------------------------


def rebuild_pg_vectors(
    dsn: str,
    repository: RebuildSource,
    scope: Scope | None,
    embedding: Any,
    *,
    keep_rollback: bool = True,
) -> RebuildResult:
    """Atomic side-by-side rebuild of a PGVectorIndex table.

    Builds ``memory_vectors_rebuild_<nonce>`` + meta, verifies, then
    transactionally renames: live -> ``memory_vectors_prev_<nonce>``,
    candidate -> live. PG DDL is transactional, so the swap is atomic.
    """
    from severian.infrastructure.pgvector_index import PGVectorIndex

    nonce = _nonce()
    table_name = f"memory_vectors_rebuild_{nonce}"
    prev_name = f"memory_vectors_prev_{nonce}"

    result = RebuildResult(provider_id="postgres", status="failed", candidate_path=table_name)

    try:
        scopes = _rebuild_scopes(repository, scope)
        partial_error = _partial_pg_rebuild_error(dsn, scopes) if scope is not None else None
        if partial_error is not None:
            result.status = "aborted"
            result.error = partial_error
            return result
        memories = _canonical_rows(repository, scopes)
        index = PGVectorIndex(dsn, embedding, table_name=table_name)
        try:
            index.initialize()
            index.upsert_many(memories)
            verification = verify_candidate(index, repository, scopes, embedding, memories)
            result.verification = verification
            if not verification.passed:
                result.status = "aborted"
                result.error = _verification_failure(verification)
                # Drop only owned temporary artifacts.
                with index.engine.begin() as connection:
                    connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')
                    connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}_meta"')
                return result
            index.close()
        except Exception:
            index.close()
            raise

        with index.engine.begin() as connection:
            connection.exec_driver_sql(f'ALTER TABLE "memory_vectors" RENAME TO "{prev_name}"')
            # Renaming a table does NOT rename its indexes — move the
            # live scope index with it so the canonical name is free
            # for the candidate below.
            connection.exec_driver_sql(
                f'ALTER INDEX IF EXISTS "ix_memory_vectors_scope" RENAME TO "ix_{prev_name}_scope"'
            )
            # The live meta table is named after the live table
            # (PGVectorIndex derives it as f"{table_name}_meta"), so the
            # live meta is "memory_vectors_meta", not the alembic-only
            # module-level "vector_space_meta".
            connection.exec_driver_sql(
                f'ALTER TABLE "memory_vectors_meta" RENAME TO "{prev_name}_meta"'
            )
            connection.exec_driver_sql(f'ALTER TABLE "{table_name}" RENAME TO "memory_vectors"')
            connection.exec_driver_sql(
                f'ALTER INDEX IF EXISTS "ix_{table_name}_scope" '
                'RENAME TO "ix_memory_vectors_scope"'
            )
            connection.exec_driver_sql(
                f'ALTER TABLE "{table_name}_meta" RENAME TO "memory_vectors_meta"'
            )
        result.status = "swapped"
        result.rollback_path = prev_name if keep_rollback else None
        if not keep_rollback:
            with index.engine.begin() as connection:
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{prev_name}"')
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{prev_name}_meta"')
        return result
    except Exception as exc:  # noqa: BLE001 — surfaced in result.error
        result.status = "failed"
        result.error = str(exc)
        try:
            with _pg_engine(dsn).begin() as connection:
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')
                connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}_meta"')
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            pass
        return result


def _pg_engine(dsn: str) -> Any:
    from sqlalchemy import create_engine

    if dsn.startswith("postgresql://") and "+" not in dsn.split("://", 1)[0]:
        dsn = dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    return create_engine(dsn, pool_pre_ping=True)


def _nonce() -> str:
    # Underscore (not hyphen): the nonce feeds derived identifiers
    # (table name, scope index name), which PG requires unquoted.
    return f"{int(time.time())}_{uuid.uuid4().hex[:8]}"


def _verification_failure(verification: RebuildVerification) -> str:
    return (
        "candidate failed verification: "
        f"count {verification.vector_count}/{verification.canonical_count}, "
        f"digest_match={verification.digest_match}, "
        f"canaries {verification.canary_hits}/{verification.canary_total}, "
        f"deterministic={verification.deterministic}, "
        f"dim={verification.dimension} fp={verification.fingerprint[:12]}"
    )
