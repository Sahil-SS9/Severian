"""PostgreSQL/pgvector backend for Severian (conformance target).

This module defines the PostgreSQL implementation of the
:class:`~severian.backends.CanonicalRepository` contract. It is the
future backend target that lets Severian run on PostgreSQL with
pgvector instead of SQLite.

The implementation is a structured stub: it documents the exact SQL
schema and query obligations a conformant PostgreSQL backend must
satisfy, and it is importable without a live database so the
conformance suite can statically verify it satisfies the contract. The
actual psycopg/asyncpg connection wiring and migration scripts are
delivered separately once a target PostgreSQL instance is provisioned.

Scope invariants every conformant backend must uphold (enforced by the
conformance suite in ``tests/contract``):

- All scoped reads filter on ``tenant_id``, ``profile_id``,
  ``collection_id``, ``agent_id`` and ``session_id`` exactly. A missing
  or mismatched scope raises ``NotFoundError``; the backend must never
  reveal whether a resource exists in another scope.
- ``pgvector`` columns store derived embeddings; they are projections
  and are never the authoritative record. Canonical truth lives in the
  ``records`` table (paralleling ``SQLiteRepository``) or an equivalent
  normalised schema with identical scope columns.
- Blank scope dimensions are rejected before any query executes.
- Immutable source observations are never overwritten; revisions create
  new rows and set ``superseded_by_id`` on the prior row.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from severian.domain.models import (
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
    Scope,
)

# SQL obligations for a conformant PostgreSQL backend. These are the
# statements a migration must create; the conformance suite references
# them as the contract's reference schema.
REFERENCE_SCHEMA_SQL: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS records (
        id TEXT PRIMARY KEY,
        record_type TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        profile_id TEXT NOT NULL DEFAULT '',
        collection_id TEXT NOT NULL,
        agent_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        content_hash TEXT,
        identity_key TEXT,
        status TEXT,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_records_type ON records (record_type);",
    "CREATE INDEX IF NOT EXISTS ix_records_tenant ON records (tenant_id);",
    "CREATE INDEX IF NOT EXISTS ix_records_collection ON records (collection_id);",
    "CREATE INDEX IF NOT EXISTS ix_records_status ON records (status);",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_records_scoped_identity
    ON records (record_type, tenant_id, profile_id, collection_id,
                agent_id, session_id, identity_key)
    WHERE identity_key IS NOT NULL;
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_vectors (
        memory_id TEXT PRIMARY KEY,
        tenant_id TEXT NOT NULL,
        profile_id TEXT NOT NULL DEFAULT '',
        collection_id TEXT NOT NULL,
        agent_id TEXT NOT NULL DEFAULT '',
        session_id TEXT NOT NULL DEFAULT '',
        embedding vector(256),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    "CREATE INDEX IF NOT EXISTS ix_memory_vectors_scope ON memory_vectors "
    "(tenant_id, profile_id, collection_id, agent_id, session_id);",
]


# Scope predicate obligation, expressed as a SQL fragment the backend
# must include on every scoped SELECT. The conformance suite asserts the
# backend's generated SQL contains this shape.
SCOPE_PREDICATE_SQL = (
    "tenant_id = %(tenant_id)s AND profile_id = %(profile_id)s "
    "AND collection_id = %(collection_id)s AND agent_id = %(agent_id)s "
    "AND session_id = %(session_id)s"
)


class PostgreSQLBackend:
    """Reference PostgreSQL backend skeleton.

    This class satisfies the :class:`CanonicalRepository` protocol
    structurally. The live constructor would accept a SQLAlchemy
    ``Engine``/connection pool; for conformance purposes it is
    constructed without a database and only validates the contract
    surface and scope invariants.

    It intentionally raises ``NotImplementedError`` for data-access
    methods so that running it against a missing database fails
    closed rather than silently returning empty results.
    """

    def __init__(self, *, dsn: str | None = None) -> None:
        self._dsn = dsn

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        raise NotImplementedError("PostgreSQL backend requires a live DSN to initialize")

    def close(self) -> None:
        raise NotImplementedError("PostgreSQL backend requires a live DSN to close")

    # -- collections -------------------------------------------------------

    def create_collection(self, collection: Collection) -> Collection:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def get_collection(self, tenant_id: str, collection_id: str) -> Collection:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    # -- observations ------------------------------------------------------

    def add_observation(self, observation: Observation) -> Observation:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def get_observation(self, scope: Scope, observation_id: str) -> Observation:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def list_observations(self, scope: Scope) -> list[Observation]:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    # -- memories ----------------------------------------------------------

    def add_memory(self, memory: Memory) -> Memory:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def get_memory(self, scope: Scope, memory_id: str) -> Memory:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def list_memories(self, scope: Scope) -> list[Memory]:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    # -- derivation jobs ---------------------------------------------------

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def claim_job(self, worker_id: str) -> Job:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    # -- entities and relations --------------------------------------------

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def get_entity(self, scope: Scope, entity_id: str) -> Entity:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def add_relation(
        self,
        scope: Scope,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> Relation:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    # -- audit and recovery ------------------------------------------------

    def append_audit(self, scope: Scope, action: str, target_id: str, metadata: Mapping[str, Any]) -> Any:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def append_outbox(self, scope: Scope, kind: str, payload: Mapping[str, Any]) -> Event:
        self._require_scope(scope)
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def pending_outbox(self) -> list[Event]:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def export_json(self) -> str:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def import_json(self, payload: str) -> None:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    def backup(self, destination: Any) -> None:
        raise NotImplementedError("PostgreSQL backend requires a live DSN")

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _require_scope(scope: Scope) -> Scope:
        """Fail closed on blank scope before touching storage."""
        if not scope.tenant_id.strip():
            raise ValueError("scope dimension tenant_id must not be blank")
        if not scope.profile_id.strip():
            raise ValueError("scope dimension profile_id must not be blank")
        if not scope.collection_id.strip():
            raise ValueError("scope dimension collection_id must not be blank")
        if not scope.agent_id.strip():
            raise ValueError("scope dimension agent_id must not be blank")
        if not scope.session_id.strip():
            raise ValueError("scope dimension session_id must not be blank")
        return scope


__all__ = [
    "PostgreSQLBackend",
    "REFERENCE_SCHEMA_SQL",
    "SCOPE_PREDICATE_SQL",
]
