"""pgvector-backed vector projection index for Severian.

Mirrors the duck-typed surface of ``LocalVectorIndex`` (``upsert``,
``delete``, ``count``, ``clear``, ``search``, ``rebuild``, ``close``)
so it can be swapped into ``ProjectionCoordinator`` and
``RetrievalService`` without changing the application layer.

Vectors are produced by the same deterministic :class:`HashEmbedding`
used by the SQLite reference, so both backends index the same
content the same way and produce comparable results. The pgvector
``<=>`` (cosine distance) operator performs ranking inside Postgres
instead of fetching all rows into Python.

The table carries the five scope dimensions as indexed columns so
every search is scoped server-side; a blank scope is rejected before
any query executes (same fail-closed rule as the canonical backend).
"""
from __future__ import annotations

import json

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Index, MetaData, String, Table, create_engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from severian.application.ports import CanonicalRepository
from severian.domain.hits import VectorHit
from severian.domain.models import Memory, Scope
from severian.infrastructure.embedding_metadata import (
    EmbeddingMetadata,
    EmbeddingSpaceMismatchError,
    metadata_from_dict,
)
from severian.infrastructure.embedding_registry import resolve_embedding_metadata
from severian.infrastructure.vector import EmbeddingProvider, HashEmbedding

# Default vector dimension (hash embeddings). The table is built by
# :func:`_build_memory_vectors` so a semantic embedding with a different
# dimension (e.g. Granite R2 at 384) creates its own schema.
DEFAULT_VECTOR_DIMENSION = 256


def _build_memory_vectors(dimension: int, *, name: str = "memory_vectors") -> Table:
    """Build a ``memory_vectors``-shaped table for a vector dimension."""
    meta = MetaData()
    table = Table(
        name,
        meta,
        Column("memory_id", String, primary_key=True),
        Column("tenant_id", String, nullable=False),
        Column("profile_id", String, nullable=False),
        Column("collection_id", String, nullable=False),
        Column("agent_id", String, nullable=False),
        Column("session_id", String, nullable=False),
        Column("content", String, nullable=False),
        Column("vector", Vector(dimension), nullable=False),
    )
    Index(
        f"ix_{name}_scope",
        table.c.tenant_id,
        table.c.profile_id,
        table.c.collection_id,
        table.c.agent_id,
        table.c.session_id,
    )
    return table


def _build_vector_space_meta(*, name: str = "vector_space_meta") -> Table:
    """Key/value table holding the embedding-space identity."""
    meta = MetaData()
    return Table(
        name,
        meta,
        Column("key", String, primary_key=True),
        Column("value", String, nullable=False),
    )


# Module-level default schema for alembic (migrations/env.py merges this
# into the PG migration target). The default memory_vectors table and
# its meta table live here so the vector index is versioned alongside
# the canonical store; side-by-side rebuild lanes build their own named
# tables via PGVectorIndex(table_name=...).
metadata = MetaData()
memory_vectors = _build_memory_vectors(DEFAULT_VECTOR_DIMENSION, name="memory_vectors")
memory_vectors.metadata = metadata  # rebind onto the module metadata
metadata._add_table("memory_vectors", None, memory_vectors)
vector_space_meta = _build_vector_space_meta(name="vector_space_meta")
vector_space_meta.metadata = metadata
metadata._add_table("vector_space_meta", None, vector_space_meta)


class PGVectorIndex:
    """Vector projection stored as real pgvector columns.

    The vector column dimension is derived from the embedding provider
    (default 256 for :class:`HashEmbedding`; 384 for Granite R2). The
    table is built per-instance so a semantic embedding creates its own
    schema instead of colliding with the hash schema.
    """

    def __init__(
        self,
        dsn: str,
        embedding: EmbeddingProvider | None = None,
        *,
        table_name: str = "memory_vectors",
    ):
        if not dsn or not dsn.strip():
            raise ValueError("PGVectorIndex requires a non-empty DSN")
        self.dsn = dsn.strip()
        if self.dsn.startswith("postgresql://") and "+" not in self.dsn.split("://", 1)[0]:
            self.dsn = self.dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        self.embedding = embedding or HashEmbedding()
        self._dimension = int(getattr(self.embedding, "dimension", DEFAULT_VECTOR_DIMENSION))
        self.table_name = table_name
        self._table = _build_memory_vectors(self._dimension, name=table_name)
        self._meta = _build_vector_space_meta(name=f"{table_name}_meta")
        self._metadata = resolve_embedding_metadata(self.embedding)
        self.engine = create_engine(self.dsn, pool_pre_ping=True)

    def initialize(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            self._table.metadata.create_all(connection)
            self._meta.metadata.create_all(connection)
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS "
                f"ix_{self.table_name}_scope "
                f"ON {self.table_name} (tenant_id, profile_id, collection_id, agent_id, session_id)"
            )
        self._stamp_or_verify_space()

    def _stamp_or_verify_space(self) -> None:
        """Stamp an empty PG index; verify a non-empty one (see LocalVectorIndex)."""
        with self.engine.begin() as connection:
            row = connection.execute(
                select(self._meta.c.value).where(self._meta.c.key == "embedding_metadata")
            ).scalar_one_or_none()
            count = int(connection.execute(
                select(func.count()).select_from(self._table)
            ).scalar_one())
            if row is None:
                if count > 0:
                    raise EmbeddingSpaceMismatchError(
                        stored=EmbeddingMetadata(
                            provider="unknown", model_id="unknown", revision="unknown",
                            dimension=0, normalize=False, pooling="unknown",
                            prompt_contract="", runtime="unknown"),
                        active=self._metadata,
                        path_or_dsn=self.dsn,
                    )
                connection.execute(
                    self._meta.insert().values(
                        key="embedding_metadata",
                        value=json.dumps(self._metadata.to_dict(), sort_keys=True),
                    )
                )
                return
            stored = metadata_from_dict(json.loads(row))
            if stored.fingerprint != self._metadata.fingerprint:
                if count == 0:
                    # Empty index: no data at risk — restamp with the
                    # active space (see LocalVectorIndex).
                    connection.execute(
                        self._meta.update().where(self._meta.c.key == "embedding_metadata").values(
                            value=json.dumps(self._metadata.to_dict(), sort_keys=True),
                        )
                    )
                    return
                raise EmbeddingSpaceMismatchError(
                    stored=stored, active=self._metadata, path_or_dsn=self.dsn
                )

    def reset(self) -> None:
        with self.engine.begin() as connection:
            self._table.metadata.drop_all(connection)
        self.initialize()

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _require_scope(scope: Scope) -> Scope:
        for dimension in ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id"):
            if not getattr(scope, dimension).strip():
                raise ValueError(f"scope dimension {dimension} must not be blank")
        return scope

    def upsert(self, memory: Memory) -> None:
        vector = self.embedding.encode(memory.content)
        values = {
            "memory_id": memory.id,
            **memory.scope.model_dump(),
            "content": memory.content,
            "vector": vector,
        }
        with self.engine.begin() as connection:
            connection.execute(
                pg_insert(self._table).values(**values).on_conflict_do_update(
                    index_elements=["memory_id"],
                    set_={
                        "tenant_id": values["tenant_id"],
                        "profile_id": values["profile_id"],
                        "collection_id": values["collection_id"],
                        "agent_id": values["agent_id"],
                        "session_id": values["session_id"],
                        "content": values["content"],
                        "vector": values["vector"],
                    },
                )
            )

    def upsert_many(self, memories: list[Memory]) -> None:
        """Bulk upsert in ONE transaction (zero-debt 3.3).

        The rebuild/batch path must not commit per row — one transaction
        for the whole batch via a single multi-row INSERT .. ON CONFLICT.
        Atomicity: any failure rolls back the batch.
        """
        if not memories:
            return
        rows = []
        for memory in memories:
            rows.append(
                {
                    "memory_id": memory.id,
                    **memory.scope.model_dump(),
                    "content": memory.content,
                    "vector": self.embedding.encode(memory.content),
                }
            )
        statement = pg_insert(self._table).values(rows)
        statement = statement.on_conflict_do_update(
            index_elements=["memory_id"],
            set_={
                "tenant_id": statement.excluded.tenant_id,
                "profile_id": statement.excluded.profile_id,
                "collection_id": statement.excluded.collection_id,
                "agent_id": statement.excluded.agent_id,
                "session_id": statement.excluded.session_id,
                "content": statement.excluded.content,
                "vector": statement.excluded.vector,
            },
        )
        with self.engine.begin() as connection:
            connection.execute(statement)

    def delete(self, memory_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(self._table.delete().where(self._table.c.memory_id == memory_id))
    def count(self, scope: Scope) -> int:
        self._require_scope(scope)
        with self.engine.connect() as connection:
            return int(
                connection.execute(
                    select(func.count()).select_from(self._table).where(
                        *(self._table.c[key] == value for key, value in scope.model_dump().items())
                    )
                ).scalar_one()
            )

    def clear(self, scope: Scope | None = None) -> None:
        with self.engine.begin() as connection:
            if scope is None:
                connection.execute(self._table.delete())
            else:
                self._require_scope(scope)
                connection.execute(
                    self._table.delete().where(
                        *(self._table.c[key] == value for key, value in scope.model_dump().items())
                    )
                )

    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[VectorHit]:
        self._require_scope(scope)
        query_vector = self.embedding.encode(query)
        distance = self._table.c.vector.cosine_distance(query_vector)
        stmt = (
            select(
                self._table.c.memory_id,
                (1.0 - distance).label("score"),
            )
            .where(*(self._table.c[key] == value for key, value in scope.model_dump().items()))
            .order_by(distance, self._table.c.content, self._table.c.memory_id)
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(stmt).mappings()
            return [VectorHit(memory_id=row["memory_id"], score=float(row["score"])) for row in rows]

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        self.clear(scope)
        self.upsert_many(list(repository.list_memories(scope)))

    def digest(self, scope: Scope) -> str:
        """Stable digest over (memory_id, content) pairs in scope."""
        import hashlib

        with self.engine.connect() as connection:
            rows = connection.execute(
                select(self._table.c.memory_id, self._table.c.content)
                .where(
                    *(
                        self._table.c[key] == value
                        for key, value in scope.model_dump().items()
                    )
                )
                .order_by(self._table.c.memory_id)
            ).all()
        digest = hashlib.sha256()
        for memory_id, content in rows:
            digest.update(str(memory_id).encode())
            digest.update(b"\x00")
            digest.update(str(content).encode())
            digest.update(b"\x00")
        return digest.hexdigest()

    def health(self) -> str:
        """Adapter-neutral probe: returns ``ok`` or ``error``."""
        try:
            with self.engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
            return "ok"
        except Exception:  # pragma: no cover - defensive
            return "error"


__all__ = ["PGVectorIndex"]
