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

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Index, MetaData, String, Table, create_engine, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Scope
from severian.infrastructure.vector import HashEmbedding, VectorHit

metadata = MetaData()

memory_vectors = Table(
    "memory_vectors",
    metadata,
    Column("memory_id", String, primary_key=True),
    Column("tenant_id", String, nullable=False),
    Column("profile_id", String, nullable=False),
    Column("collection_id", String, nullable=False),
    Column("agent_id", String, nullable=False),
    Column("session_id", String, nullable=False),
    Column("content", String, nullable=False),
    Column("vector", Vector(256), nullable=False),
)

Index(
    "ix_memory_vectors_scope",
    memory_vectors.c.tenant_id,
    memory_vectors.c.profile_id,
    memory_vectors.c.collection_id,
    memory_vectors.c.agent_id,
    memory_vectors.c.session_id,
)


class PGVectorIndex:
    """Vector projection stored as real ``vector(256)`` columns via pgvector."""

    def __init__(self, dsn: str, embedding: HashEmbedding | None = None):
        if not dsn or not dsn.strip():
            raise ValueError("PGVectorIndex requires a non-empty DSN")
        self.dsn = dsn.strip()
        if self.dsn.startswith("postgresql://") and "+" not in self.dsn.split("://", 1)[0]:
            self.dsn = self.dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        self.embedding = embedding or HashEmbedding()
        self.engine = create_engine(self.dsn, pool_pre_ping=True)

    def initialize(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            metadata.create_all(connection)
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_memory_vectors_scope "
                "ON memory_vectors (tenant_id, profile_id, collection_id, agent_id, session_id)"
            )

    def reset(self) -> None:
        with self.engine.begin() as connection:
            metadata.drop_all(connection)
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
                pg_insert(memory_vectors).values(**values).on_conflict_do_update(
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

    def delete(self, memory_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(memory_vectors.delete().where(memory_vectors.c.memory_id == memory_id))

    def count(self, scope: Scope) -> int:
        self._require_scope(scope)
        with self.engine.connect() as connection:
            value = connection.execute(
                select(func.count()).select_from(memory_vectors).where(
                    *(memory_vectors.c[key] == value for key, value in scope.model_dump().items())
                )
            ).scalar_one()
        return int(value)

    def clear(self, scope: Scope | None = None) -> None:
        with self.engine.begin() as connection:
            if scope is None:
                connection.execute(memory_vectors.delete())
            else:
                self._require_scope(scope)
                connection.execute(
                    memory_vectors.delete().where(
                        *(memory_vectors.c[key] == value for key, value in scope.model_dump().items())
                    )
                )

    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[VectorHit]:
        self._require_scope(scope)
        query_vector = self.embedding.encode(query)
        distance = memory_vectors.c.vector.cosine_distance(query_vector)
        stmt = (
            select(
                memory_vectors.c.memory_id,
                (1.0 - distance).label("score"),
            )
            .where(*(memory_vectors.c[key] == value for key, value in scope.model_dump().items()))
            .order_by(distance)
            .limit(limit)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(stmt).mappings()
            return [VectorHit(memory_id=row["memory_id"], score=float(row["score"])) for row in rows]

    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None:
        self.clear(scope)
        for memory in repository.list_memories(scope):
            self.upsert(memory)

    def digest(self, scope: Scope) -> str:
        """Stable digest over (memory_id, content) pairs in scope."""
        import hashlib

        with self.engine.connect() as connection:
            rows = connection.execute(
                select(memory_vectors.c.memory_id, memory_vectors.c.content)
                .where(
                    *(
                        memory_vectors.c[key] == value
                        for key, value in scope.model_dump().items()
                    )
                )
                .order_by(memory_vectors.c.memory_id)
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
