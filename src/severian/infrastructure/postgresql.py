"""PostgreSQL/pgvector backend for Severian (live implementation).

This module implements :class:`~severian.backends.CanonicalRepository`
against a real PostgreSQL instance with the pgvector extension. It
mirrors ``SQLiteRepository`` semantics exactly:

- The ``records`` table stores canonical rows with a JSONB ``payload``
  and the five scope columns as first-class indexed columns.
- Scoped reads filter on ``tenant_id``, ``profile_id``,
  ``collection_id``, ``agent_id`` and ``session_id`` exactly. A missing
  or mismatched scope raises ``NotFoundError``; the backend never
  reveals whether a resource exists in another scope.
- Idempotent writes use a partial unique index on the scoped identity
  key and ``INSERT ... ON CONFLICT DO NOTHING``, then re-select the
  existing row (same behaviour as the SQLite reference).
- Blank scope dimensions are rejected before any query executes.
- Immutable source observations are never overwritten; revisions create
  new rows and set ``superseded_by_id`` on the prior row.

``initialize`` creates the extension, schema, and indexes. It is
safe to call on an already-initialised database.

The DSN is a standard libpq URL, e.g. ``postgresql://user:pass@host:5432/db``.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar, cast

from sqlalchemy import Column, Index, MetaData, String, Table, create_engine, inspect, or_, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import (
    AuditEvent,
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
    Scope,
    source_identity,
)
from severian.infrastructure.snapshot import validate_snapshot

T = TypeVar("T")


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 UTC string for column comparison (lexicographic == chronological)."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


def _migrations_dir() -> Path:
    """Locate the Alembic migration tree relative to the package root."""
    return Path(__file__).resolve().parent.parent.parent.parent / "migrations"


metadata = MetaData()

# The canonical table mirrors SQLiteRepository's ``records`` exactly so
# that exports are byte-compatible across backends (same columns, same
# payload JSON). ``created_at`` lives inside the payload, matching the
# SQLite reference; it is not a separate column here.
records = Table(
    "records",
    metadata,
    Column("id", String, primary_key=True),
    Column("record_type", String, nullable=False, index=True),
    Column("tenant_id", String, index=True),
    Column("profile_id", String),
    Column("collection_id", String, index=True),
    Column("agent_id", String),
    Column("session_id", String),
    Column("content_hash", String),
    Column("identity_key", String),
    Column("status", String, index=True),
    Column("lease_until", String),
    Column("retry_after", String),
    Column("payload", JSONB, nullable=False),
)

Index(
    "uq_records_scoped_identity",
    records.c.record_type,
    records.c.tenant_id,
    records.c.profile_id,
    records.c.collection_id,
    records.c.agent_id,
    records.c.session_id,
    records.c.identity_key,
    unique=True,
    postgresql_where=records.c.identity_key.is_not(None),
)


class PostgreSQLBackend:
    def __init__(self, *, dsn: str):
        if not dsn or not dsn.strip():
            raise ValueError("PostgreSQL backend requires a non-empty DSN")
        # Default SQLAlchemy's postgresql:// dialect to psycopg3 (the
        # bundled driver) unless the caller pinned a specific dialect.
        self.dsn = dsn.strip()
        if self.dsn.startswith("postgresql://") and "+" not in self.dsn.split("://", 1)[0]:
            self.dsn = self.dsn.replace("postgresql://", "postgresql+psycopg://", 1)
        self.engine: Engine = create_engine(self.dsn, pool_pre_ping=True)

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None:
        with self.engine.begin() as connection:
            connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
            metadata.create_all(connection)
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_records_type ON records (record_type)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_records_tenant ON records (tenant_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_records_collection ON records (collection_id)"
            )
            connection.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_records_status ON records (status)"
            )

    def verify_schema(self) -> None:
        """Fail startup on schema mismatch (P1-3).

        Compares the migrated schema version (alembic_version) against
        the head of the versioned migration tree. A database that has
        never been migrated (no alembic_version row) is treated as a
        fresh install managed by ``create_all`` and passes; a database
        whose version lags the head is refused so a deploy can never
        silently run against a stale schema.
        """
        from alembic import command
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        config = Config()
        config.set_main_option("script_location", str(_migrations_dir()))
        config.set_main_option("sqlalchemy.url", self.dsn)
        script = ScriptDirectory.from_config(config)
        head = script.get_current_head()
        if head is None:
            raise RuntimeError("no migration head found; broken migrations tree")

        with self.engine.connect() as connection:
            has_version_table = inspect(connection).has_table("alembic_version")
            version = None
            if has_version_table:
                version = connection.execute(
                    sa_text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()

        if version is None:
            # Fresh install (or pre-migration database): initialize()
            # creates the schema and stamps the head so future deploys
            # see the migrated state.
            command.stamp(config, head)
            return
        if version != head:
            raise RuntimeError(
                f"PostgreSQL schema version {version!r} does not match migration head {head!r}; "
                "run `alembic upgrade head` before starting"
            )

    def reset(self, *, acknowledge: bool = False) -> None:
        """Drop and recreate the schema (test/scratch use only).

        P1-3: destructive by design. Requires explicit acknowledgement
        AND a test-owned database (name contains ``test`` or the
        ``SEVERIAN_PG_ALLOW_RESET`` env var is set) so a production DSN
        can never be silently wiped. Also drops pgvector projection
        metadata (``memory_vectors``), which the earlier reset did not
        clear.
        """
        if not acknowledge:
            raise ValueError("reset() requires acknowledge=True (destructive)")
        from severian.infrastructure.pgvector_index import memory_vectors as pg_vectors

        db_name = self.dsn.rsplit("/", 1)[-1].split("?", 1)[0].strip()
        allow = db_name.lower().count("test") > 0 or os.environ.get("SEVERIAN_PG_ALLOW_RESET") == "1"
        if not allow:
            raise ValueError(
                "reset() refused: database is not test-owned "
                f"(db_name={db_name!r}, SEVERIAN_PG_ALLOW_RESET not set)"
            )
        with self.engine.begin() as connection:
            pg_vectors.drop(connection, checkfirst=True)
            metadata.drop_all(connection)
        self.initialize()
        # The pgvector projection table lives in a separate metadata
        # object (the default 256-dim table); recreate it so a bundle
        # already holding a PGVectorIndex can keep writing after the
        # reset.
        with self.engine.begin() as connection:
            pg_vectors.metadata.create_all(connection)

    def close(self) -> None:
        self.engine.dispose()

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _scope_values(scope: Scope | None) -> dict[str, str | None]:
        if scope is None:
            return {key: None for key in ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")}
        return scope.model_dump()

    @staticmethod
    def _collection_scope(collection: Collection) -> dict[str, str | None]:
        return {
            "tenant_id": collection.tenant_id,
            "profile_id": "",
            "collection_id": collection.id,
            "agent_id": "",
            "session_id": "",
        }

    @staticmethod
    def _identity(*parts: object) -> str:
        encoded = json.dumps(
            parts,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def _record_values(
        cls,
        kind: str,
        model: Any,
        *,
        content_hash: str | None = None,
        identity_key: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(model, Collection):
            scope_values: dict[str, str | None] = cls._collection_scope(model)
        else:
            scope_values = cls._scope_values(getattr(model, "scope", None))
        values: dict[str, Any] = {
            "id": model.id,
            "record_type": kind,
            **scope_values,
            "content_hash": content_hash,
            "identity_key": identity_key,
            "status": status,
            "payload": model.model_dump(mode="json"),
        }
        if kind == "job":
            values["lease_until"] = _iso(model.lease_until)
            values["retry_after"] = _iso(model.retry_after)
        return values

    @staticmethod
    def _event_models(
        kind: str,
        model: Any,
        *,
        operation: str = "create",
    ) -> tuple[Event, AuditEvent] | None:
        scope = getattr(model, "scope", None)
        if scope is None:
            return None
        event_suffix = {"create": "created", "revise": "revised"}[operation]
        event = Event(scope=scope, kind=f"{kind}.{event_suffix}", payload={"id": model.id})
        audit = AuditEvent(scope=scope, action=f"{kind}.{operation}", target_id=model.id, metadata={})
        return event, audit

    @staticmethod
    def _scope_predicates(scope: Scope) -> list[Any]:
        return [getattr(records.c, key) == value for key, value in scope.model_dump().items()]

    def _insert(
        self,
        kind: str,
        model: T,
        *,
        content_hash: str | None = None,
        identity_key: str | None = None,
        status: str | None = None,
        emit_events: bool = False,
    ) -> T:
        values = self._record_values(
            kind,
            model,
            content_hash=content_hash,
            identity_key=identity_key,
            status=status,
        )
        with self.engine.begin() as connection:
            connection.execute(records.insert().values(**values))
            if emit_events and (events := self._event_models(kind, model)) is not None:
                event, audit = events
                connection.execute(records.insert().values(**self._record_values("outbox", event, status="pending")))
                connection.execute(records.insert().values(**self._record_values("audit", audit)))
        return model

    def _insert_if_absent(
        self,
        kind: str,
        model: T,
        *,
        identity_key: str,
        content_hash: str | None = None,
        status: str | None = None,
        emit_events: bool = False,
        query: Any,
    ) -> T:
        """Insert idempotently: on identity conflict, re-select the existing row.

        Mirrors SQLiteRepository's IntegrityError-catch-and-reload, so
        both backends behave identically under identity conflicts.
        """
        values = self._record_values(
            kind,
            model,
            content_hash=content_hash,
            identity_key=identity_key,
            status=status,
        )
        try:
            with self.engine.begin() as connection:
                connection.execute(records.insert().values(**values))
                if emit_events and (events := self._event_models(kind, model)) is not None:
                    event, audit = events
                    connection.execute(
                        records.insert().values(**self._record_values("outbox", event, status="pending"))
                    )
                    connection.execute(records.insert().values(**self._record_values("audit", audit)))
            return model
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(query).scalar_one_or_none()
            if existing is None:
                raise ConflictError(f"{kind} identity conflict") from None
            return cast(T, cast(Any, type(model)).model_validate(existing))

    def _get(self, kind: str, record_id: str, model: type[T], *, scope: Scope | None = None) -> T:
        predicates: list[Any] = [records.c.id == record_id, records.c.record_type == kind]
        if scope is not None:
            predicates.extend(self._scope_predicates(scope))
        with self.engine.connect() as connection:
            row = connection.execute(select(records.c.payload).where(*predicates)).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"{kind} not found")
        return model.model_validate(row)  # type: ignore[attr-defined,no-any-return]

    @staticmethod
    def _insert_events_in_transaction(
        connection: Connection,
        kind: str,
        model: Any,
        *,
        operation: str = "create",
    ) -> None:
        if (events := PostgreSQLBackend._event_models(kind, model, operation=operation)) is None:
            return
        event, audit = events
        connection.execute(
            records.insert().values(**PostgreSQLBackend._record_values("outbox", event, status="pending"))
        )
        connection.execute(records.insert().values(**PostgreSQLBackend._record_values("audit", audit)))

    # -- collections -------------------------------------------------------

    def create_collection(self, collection: Collection) -> Collection:
        return self._insert("collection", collection, identity_key=self._identity(collection.id))

    def get_collection(self, tenant_id: str, collection_id: str) -> Collection:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(records.c.payload).where(
                    records.c.id == collection_id,
                    records.c.record_type == "collection",
                    records.c.tenant_id == tenant_id,
                    records.c.collection_id == collection_id,
                )
            ).scalar_one_or_none()
        if row is None:
            raise NotFoundError("collection not found")
        return Collection.model_validate(row)

    # -- observations ------------------------------------------------------

    def add_observation(self, observation: Observation) -> Observation:
        self._require_scope(observation.scope)
        digest = hashlib.sha256(observation.content.encode()).hexdigest()
        # P0-1: preserve immutable raw evidence. Direct constructions that
        # omit raw fields are backfilled so the stored record always has a
        # raw_hash; the raw pair is never rewritten on revision.
        raw = observation.raw_content or observation.content
        raw_hash = observation.raw_hash or hashlib.sha256(raw.encode()).hexdigest()
        candidate = observation.model_copy(
            update={"content_hash": digest, "raw_content": raw, "raw_hash": raw_hash}
        )
        # Dedupe on stable source identity + content digest, never digest
        # alone: ``"1"`` from note://A and ``"1"`` from note://B are
        # distinct evidence and must not collapse (P0-1).
        identity_parts: tuple[object, ...] = (digest,)
        source = source_identity(candidate.source)
        if source:
            identity_parts = (digest, source)
        identity_key = self._identity(*identity_parts)
        query = select(records.c.payload).where(
            records.c.record_type == "observation",
            *self._scope_predicates(candidate.scope),
            records.c.identity_key == identity_key,
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Observation.model_validate(existing)
        return self._insert_if_absent(
            "observation",
            candidate,
            content_hash=digest,
            identity_key=identity_key,
            emit_events=True,
            query=query,
        )

    def get_observation(self, scope: Scope, observation_id: str) -> Observation:
        self._require_scope(scope)
        return self._get("observation", observation_id, Observation, scope=scope)

    def add_observation_and_enqueue(
        self, observation: Observation, job_kind: str = "derive"
    ) -> tuple[Observation, Job]:
        """Atomically insert an observation and enqueue its derive job.

        P0-4: the canonical write and the queue write are ONE
        transaction. Identity semantics match ``add_observation`` +
        ``enqueue_job`` so re-ingests stay idempotent.
        """
        digest = hashlib.sha256(observation.content.encode()).hexdigest()
        raw = observation.raw_content or observation.content
        raw_hash = observation.raw_hash or hashlib.sha256(raw.encode()).hexdigest()
        candidate = observation.model_copy(
            update={"content_hash": digest, "raw_content": raw, "raw_hash": raw_hash}
        )
        identity_parts: tuple[object, ...] = (digest,)
        source = source_identity(candidate.source)
        if source:
            identity_parts = (digest, source)
        observation_identity = self._identity(*identity_parts)
        job_payload = {"observation_id": candidate.id}
        job_identity = self._identity(job_kind, job_payload)

        observation_query = select(records.c.payload).where(
            records.c.record_type == "observation",
            *self._scope_predicates(candidate.scope),
            records.c.identity_key == observation_identity,
        )
        with self.engine.connect() as connection:
            existing_obs = connection.execute(observation_query).scalar_one_or_none()
        if existing_obs is not None:
            # Re-ingest of the same evidence: return the stored
            # observation and its existing derive job (enqueueing it
            # again if a previous atomic write somehow lost it).
            stored = Observation.model_validate(existing_obs)
            job_payload = {"observation_id": stored.id}
            job_identity = self._identity(job_kind, job_payload)
            job_query = select(records.c.payload).where(
                records.c.record_type == "job",
                *self._scope_predicates(stored.scope),
                records.c.identity_key == job_identity,
            )
            with self.engine.connect() as connection:
                existing_job = connection.execute(job_query).scalar_one_or_none()
            if existing_job is not None:
                return stored, Job.model_validate(existing_job)
            job = Job(scope=stored.scope, kind=job_kind, payload=job_payload)
            try:
                return stored, self._insert_if_absent(
                    "job",
                    job,
                    identity_key=job_identity,
                    status="pending",
                    query=job_query,
                )
            except IntegrityError:
                with self.engine.connect() as connection:
                    existing_job = connection.execute(job_query).scalar_one_or_none()
                if existing_job is None:
                    raise ConflictError("job identity conflict") from None
                return stored, Job.model_validate(existing_job)

        job = Job(scope=candidate.scope, kind=job_kind, payload={"observation_id": candidate.id})
        job_identity = self._identity(job_kind, job.payload)
        with self.engine.begin() as connection:
            connection.execute(
                records.insert().values(
                    **self._record_values(
                        "observation",
                        candidate,
                        content_hash=digest,
                        identity_key=observation_identity,
                    )
                )
            )
            if (events := self._event_models("observation", candidate)) is not None:
                event, audit = events
                connection.execute(records.insert().values(**self._record_values("outbox", event, status="pending")))
                connection.execute(records.insert().values(**self._record_values("audit", audit)))
            connection.execute(
                records.insert().values(**self._record_values("job", job, identity_key=job_identity, status="pending"))
            )
        return candidate, job

    def list_observations(self, scope: Scope) -> list[Observation]:
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "observation", *self._scope_predicates(scope))
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [Observation.model_validate(value) for value in connection.execute(query).scalars()]

    def update_observation_content(self, observation_id: str, content: str) -> None:
        del observation_id, content
        raise ConflictError("observations are immutable; create a revision")

    def revise_observation(self, scope: Scope, observation_id: str, content: str) -> Observation:
        original = self.get_observation(scope, observation_id)
        if original.superseded_by_id is not None:
            raise ConflictError("observation is already superseded")
        digest = hashlib.sha256(content.encode()).hexdigest()
        revision = Observation(
            scope=scope,
            content=content,
            content_hash=digest,
            source=original.source,
            revision_of_id=original.id,
        )
        changed = original.model_copy(update={"superseded_by_id": revision.id})
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(
                    records.c.id == original.id,
                    records.c.record_type == "observation",
                    *self._scope_predicates(scope),
                    records.c.payload["superseded_by_id"].astext.is_(None),
                )
                .values(payload=changed.model_dump(mode="json"))
            )
            if result.rowcount != 1:
                raise ConflictError("observation revision conflict")
            connection.execute(
                records.insert().values(
                    **self._record_values(
                        "observation",
                        revision,
                        content_hash=digest,
                        identity_key=self._identity("revision", original.id, revision.id),
                    )
                )
            )
            self._insert_events_in_transaction(connection, "observation", revision, operation="revise")
        return revision

    # -- memories ----------------------------------------------------------

    def add_memory(self, memory: Memory) -> Memory:
        for source_id in memory.source_observation_ids:
            try:
                source = self.get_observation(memory.scope, source_id)
            except NotFoundError as exc:
                with self.engine.connect() as connection:
                    exists = connection.execute(
                        select(records.c.id).where(records.c.id == source_id, records.c.record_type == "observation")
                    ).scalar_one_or_none()
                if exists is not None:
                    raise ConflictError("source observation scope does not match memory scope") from exc
                raise
            if source.scope != memory.scope:
                raise ConflictError("source observation scope does not match memory scope")
        identity_key = self._identity(
            memory.kind,
            memory.content,
            memory.source_observation_ids,
            memory.model_id,
            memory.prompt_version,
        )
        query = select(records.c.payload).where(
            records.c.record_type == "memory",
            *self._scope_predicates(memory.scope),
            records.c.identity_key == identity_key,
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Memory.model_validate(existing)
        return self._insert_if_absent(
            "memory",
            memory,
            identity_key=identity_key,
            emit_events=True,
            query=query,
        )

    def add_memories_batch(self, memories: list[Memory]) -> list[Memory]:
        """Insert candidate memories transactionally (P0-4)."""
        if not memories:
            return []
        for memory in memories:
            for source_id in memory.source_observation_ids:
                try:
                    source = self.get_observation(memory.scope, source_id)
                except NotFoundError as exc:
                    with self.engine.connect() as connection:
                        exists = connection.execute(
                            select(records.c.id).where(
                                records.c.id == source_id,
                                records.c.record_type == "observation",
                            )
                        ).scalar_one_or_none()
                    if exists is not None:
                        raise ConflictError("source observation scope does not match memory scope") from exc
                    raise
                if source.scope != memory.scope:
                    raise ConflictError("source observation scope does not match memory scope")
        identities = [
            self._identity(
                m.kind,
                m.content,
                m.source_observation_ids,
                m.model_id,
                m.prompt_version,
            )
            for m in memories
        ]
        with self.engine.begin() as connection:
            existing_by_identity: dict[str, Memory] = {}
            rows = connection.execute(
                select(records.c.payload).where(
                    records.c.record_type == "memory",
                    records.c.identity_key.in_(identities),
                )
            ).scalars().all()
            for value in rows:
                memory = Memory.model_validate(value)
                key = self._identity(
                    memory.kind,
                    memory.content,
                    memory.source_observation_ids,
                    memory.model_id,
                    memory.prompt_version,
                )
                existing_by_identity[key] = memory
            results: list[Memory] = []
            for memory, identity_key in zip(memories, identities, strict=True):
                if identity_key in existing_by_identity:
                    results.append(existing_by_identity[identity_key])
                    continue
                connection.execute(
                    records.insert().values(**self._record_values("memory", memory, identity_key=identity_key))
                )
                if (events := self._event_models("memory", memory)) is not None:
                    event, audit = events
                    connection.execute(
                        records.insert().values(**self._record_values("outbox", event, status="pending"))
                    )
                    connection.execute(records.insert().values(**self._record_values("audit", audit)))
                results.append(memory)
        return results

    def get_memory(self, scope: Scope, memory_id: str) -> Memory:
        self._require_scope(scope)
        return self._get("memory", memory_id, Memory, scope=scope)

    def list_memories(self, scope: Scope) -> list[Memory]:
        """List ACTIVE memories only — superseded revisions are excluded (P0-5)."""
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(
                records.c.record_type == "memory",
                *self._scope_predicates(scope),
                records.c.payload["superseded_by_id"].astext.is_(None),
            )
            .order_by(records.c.payload["created_at"].astext, records.c.id)
        )
        with self.engine.connect() as connection:
            return [Memory.model_validate(value) for value in connection.execute(query).scalars()]

    def list_memories_history(self, scope: Scope) -> list[Memory]:
        """List ALL memories in scope, including superseded revisions."""
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "memory", *self._scope_predicates(scope))
            .order_by(records.c.payload["created_at"].astext, records.c.id)
        )
        with self.engine.connect() as connection:
            return [Memory.model_validate(value) for value in connection.execute(query).scalars()]

    def list_memory_revisions(self, scope: Scope, memory_id: str) -> list[Memory]:
        """Return the revision chain for a memory (oldest first)."""
        self._require_scope(scope)
        chain: dict[str, Memory] = {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(records.c.payload).where(
                    records.c.record_type == "memory", *self._scope_predicates(scope)
                )
            ).scalars()
            for value in rows:
                memory = Memory.model_validate(value)
                chain[memory.id] = memory
        if memory_id not in chain:
            raise NotFoundError("memory not found")
        ordered: list[Memory] = []
        current = chain[memory_id]
        while current.revision_of_id is not None and current.revision_of_id in chain:
            current = chain[current.revision_of_id]
        seen: set[str] = set()
        while current.id not in seen and current.id in chain:
            seen.add(current.id)
            ordered.append(current)
            if current.superseded_by_id is not None and current.superseded_by_id in chain:
                current = chain[current.superseded_by_id]
            else:
                break
        return ordered

    def supersede_memories_for_source(self, scope: Scope, observation_id: str) -> int:
        """Mark active memories derived from an observation as superseded (P0-5)."""
        self._require_scope(scope)
        superseded = 0
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(records.c.id, records.c.payload).where(
                    records.c.record_type == "memory",
                    *self._scope_predicates(scope),
                    records.c.payload["superseded_by_id"].astext.is_(None),
                )
            ).mappings().all()
            for row in rows:
                memory = Memory.model_validate(row["payload"])
                if observation_id not in memory.source_observation_ids:
                    continue
                connection.execute(
                    update(records)
                    .where(records.c.id == row["id"])
                    .values(
                        payload=memory.model_copy(
                            update={"superseded_by_id": f"source-rev:{observation_id}"}
                        ).model_dump(mode="json")
                    )
                )
                superseded += 1
        return superseded

    def revise_memory(
        self,
        scope: Scope,
        memory_id: str,
        *,
        content: str,
        model_id: str,
        prompt_version: str,
    ) -> Memory:
        original = self.get_memory(scope, memory_id)
        if original.superseded_by_id is not None:
            raise ConflictError("memory is already superseded")
        revision = original.model_copy(
            update={
                "id": Memory(
                    scope=scope,
                    kind=original.kind,
                    content=content,
                    source_observation_ids=original.source_observation_ids,
                    model_id=model_id,
                    prompt_version=prompt_version,
                    confidence=original.confidence,
                ).id,
                "content": content,
                "model_id": model_id,
                "prompt_version": prompt_version,
                "revision_of_id": original.id,
                "superseded_by_id": None,
                "created_at": datetime.now(original.created_at.tzinfo),
            }
        )
        changed = original.model_copy(update={"superseded_by_id": revision.id})
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(
                    records.c.id == original.id,
                    records.c.record_type == "memory",
                    *self._scope_predicates(scope),
                    records.c.payload["superseded_by_id"].astext.is_(None),
                )
                .values(payload=changed.model_dump(mode="json"))
            )
            if result.rowcount != 1:
                raise ConflictError("memory revision conflict")
            connection.execute(records.insert().values(**self._record_values("memory", revision)))
            self._insert_events_in_transaction(connection, "memory", revision, operation="revise")
        return revision

    # -- entities and relations --------------------------------------------

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity:
        self._require_scope(scope)
        identity_key = self._identity(kind, name.casefold())
        query = select(records.c.payload).where(
            records.c.record_type == "entity",
            *self._scope_predicates(scope),
            records.c.identity_key == identity_key,
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Entity.model_validate(existing)
        entity = Entity(scope=scope, kind=kind, name=name)
        return self._insert_if_absent(
            "entity",
            entity,
            identity_key=identity_key,
            emit_events=True,
            query=query,
        )

    def get_entity(self, scope: Scope, entity_id: str) -> Entity:
        self._require_scope(scope)
        return self._get("entity", entity_id, Entity, scope=scope)

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
        for endpoint_id in (subject_id, object_id):
            try:
                self._get("entity", endpoint_id, Entity, scope=scope)
            except NotFoundError as exc:
                with self.engine.connect() as connection:
                    exists = connection.execute(
                        select(records.c.id).where(records.c.id == endpoint_id, records.c.record_type == "entity")
                    ).scalar_one_or_none()
                if exists is not None:
                    raise ConflictError("relation endpoint scope does not match relation scope") from exc
                raise
        relation = Relation(
            scope=scope,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        identity_key = self._identity(subject_id, predicate, object_id, valid_from, valid_to)
        query = select(records.c.payload).where(
            records.c.record_type == "relation",
            *self._scope_predicates(scope),
            records.c.identity_key == identity_key,
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Relation.model_validate(existing)
        return self._insert_if_absent(
            "relation",
            relation,
            identity_key=identity_key,
            emit_events=True,
            query=query,
        )

    # -- derivation jobs ---------------------------------------------------

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job:
        self._require_scope(scope)
        identity_key = self._identity(kind, payload)
        query = select(records.c.payload).where(
            records.c.record_type == "job",
            *self._scope_predicates(scope),
            records.c.identity_key == identity_key,
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Job.model_validate(existing)
        job = Job(scope=scope, kind=kind, payload=payload)
        return self._insert_if_absent(
            "job",
            job,
            identity_key=identity_key,
            status="pending",
            query=query,
        )

    def claim_job(self, worker_id: str, lease_seconds: int = 300) -> Job:
        """Atomically claim one pending job with a lease (P0-4)."""
        now_value = datetime.now(UTC)
        now_iso = _iso(now_value)
        lease_iso = _iso(now_value + timedelta(seconds=lease_seconds))
        pending_id = (
            select(records.c.id)
            .where(
                records.c.record_type == "job",
                records.c.status == "pending",
                or_(
                    records.c.retry_after.is_(None),
                    records.c.retry_after <= now_iso,
                ),
            )
            .order_by(records.c.id)
            .limit(1)
            .scalar_subquery()
        )
        with self.engine.begin() as connection:
            row = connection.execute(
                update(records)
                .where(records.c.id == pending_id, records.c.status == "pending")
                .values(status="running", lease_until=lease_iso)
                .returning(records.c.id, records.c.payload)
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError("no pending job")
            prior = Job.model_validate(row["payload"])
            job = prior.model_copy(
                update={
                    "status": "running",
                    "worker_id": worker_id,
                    "attempts": prior.attempts + 1,
                    "lease_until": now_value + timedelta(seconds=lease_seconds),
                }
            )
            connection.execute(
                update(records).where(records.c.id == row["id"]).values(payload=job.model_dump(mode="json"))
            )
        return job

    def complete_job(
        self,
        job_id: str,
        status: str,
        *,
        worker_id: str | None = None,
        error: str | None = None,
    ) -> Job:
        """Mark a claimed job as done or failed (atomically, owner-checked)."""
        if status not in ("done", "failed"):
            raise ValueError("job status must be 'done' or 'failed'")
        with self.engine.begin() as connection:
            row = connection.execute(
                select(records.c.payload).where(
                    records.c.id == job_id,
                    records.c.record_type == "job",
                    records.c.status == "running",
                )
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError("job not claimed")
            job = Job.model_validate(row["payload"])
            if worker_id is not None and job.worker_id != worker_id:
                raise ConflictError("job is owned by another worker")
            update_fields: dict[str, Any] = {
                "status": status,
                "last_error": error if status == "failed" else None,
            }
            if status == "failed":
                update_fields["retry_after"] = datetime.now(UTC) + timedelta(seconds=5)
            updated = job.model_copy(update=update_fields)
            result = connection.execute(
                update(records)
                .where(
                    records.c.id == job_id,
                    records.c.record_type == "job",
                    records.c.status == "running",
                )
                .values(status=status, payload=updated.model_dump(mode="json"), retry_after=_iso(updated.retry_after))
            )
            if result.rowcount != 1:
                raise ConflictError("job completion conflict")
        return updated

    def reap_expired_jobs(self, *, now: datetime | None = None, max_attempts: int = 3) -> int:
        """Re-queue expired running jobs and dead-letter exhausted ones (P0-4)."""
        now_value = now or datetime.now(UTC)
        now_iso = _iso(now_value)
        reaped = 0
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(records.c.id, records.c.payload).where(
                    records.c.record_type == "job",
                    records.c.status == "running",
                    records.c.lease_until.is_not(None),
                    records.c.lease_until < now_iso,
                )
            ).mappings().all()
            for row in rows:
                job = Job.model_validate(row["payload"])
                if job.attempts >= max_attempts:
                    connection.execute(
                        update(records)
                        .where(records.c.id == row["id"])
                        .values(
                            status="dead_letter",
                            payload=job.model_copy(update={"dead_letter": True}).model_dump(mode="json"),
                        )
                    )
                else:
                    connection.execute(
                        update(records)
                        .where(records.c.id == row["id"])
                        .values(
                            status="pending",
                            payload=job.model_copy(
                                update={"worker_id": None, "lease_until": None}
                            ).model_dump(mode="json"),
                        )
                    )
                reaped += 1
        return reaped

    # -- audit and recovery ------------------------------------------------

    def append_outbox(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Event:
        self._require_scope(scope)
        return self._insert("outbox", Event(scope=scope, kind=kind, payload=payload), status="pending")

    def pending_outbox(self) -> list[Event]:
        with self.engine.connect() as connection:
            values = connection.execute(
                select(records.c.payload)
                .where(records.c.record_type == "outbox", records.c.status == "pending")
                .order_by(records.c.id)
            ).scalars()
            return [Event.model_validate(value) for value in values]

    def ack_outbox(self, event_id: str) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(records.c.id == event_id, records.c.record_type == "outbox", records.c.status == "pending")
                .values(status="acked")
            )
            if result.rowcount != 1:
                raise NotFoundError("outbox event not found or already acked")

    def append_audit(self, scope: Scope, action: str, target_id: str, metadata_: Mapping[str, Any]) -> AuditEvent:
        self._require_scope(scope)
        return self._insert(
            "audit",
            AuditEvent(scope=scope, action=action, target_id=target_id, metadata=dict(metadata_)),
        )

    def list_audit(self, scope: Scope) -> list[AuditEvent]:
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "audit", *self._scope_predicates(scope))
            .order_by(records.c.payload["created_at"].astext, records.c.id)
        )
        with self.engine.connect() as connection:
            return [AuditEvent.model_validate(value) for value in connection.execute(query).scalars()]

    def backup(self, destination: Any) -> None:
        """Write a portable snapshot (export document) to destination.

        The contract's ``backup`` takes a destination path. PostgreSQL
        itself does not expose a file-level backup like SQLite's
        ``sqlite3.backup``, so we persist the export document (same
        format as ``export_json``, restorable via ``import_json``) to
        the destination. Operators who need a physical backup should use
        ``pg_dump``/WAL archiving at the server level.
        """
        payload = self.export_json()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(destination)

    @staticmethod
    def export_checksum(exported_records: list[dict[str, Any]]) -> str:
        canonical = json.dumps(exported_records, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def export_json(self) -> str:
        with self.engine.connect() as connection:
            data = [dict(row) for row in connection.execute(select(records).order_by(records.c.id)).mappings()]
        document = {
            "format": "severian-v1",
            "schema_version": 1,
            "records": data,
            "checksum": self.export_checksum(data),
        }
        return json.dumps(document, sort_keys=True)

    def import_json(self, payload: str) -> None:
        document = json.loads(payload)
        if document.get("format") != "severian-v1" or document.get("schema_version") != 1:
            raise ValueError("unsupported export format or schema version")
        incoming = document.get("records")
        if not isinstance(incoming, list):
            raise ValueError("invalid export records")
        if document.get("checksum") != self.export_checksum(incoming):
            raise ValueError("export checksum mismatch")
        required = {column.name for column in records.columns}
        for item in incoming:
            if not isinstance(item, dict) or set(item) != required:
                raise ValueError("invalid export record shape")

        # Strict semantic validation BEFORE the write transaction opens:
        # parse every record against its domain model, recompute derived
        # fields (content hash, identity key), verify scope columns match
        # the payload, validate references and revision topology. This
        # closes the re-checksummed metadata-forgery hole (P0-3).
        validate_snapshot(incoming)

        identity_columns = (
            "record_type",
            "tenant_id",
            "profile_id",
            "collection_id",
            "agent_id",
            "session_id",
            "identity_key",
        )

        def identity(item: dict[str, Any]) -> tuple[Any, ...] | None:
            if item["identity_key"] is None:
                return None
            return tuple(item[column] for column in identity_columns)

        incoming_identities: dict[tuple[Any, ...], str] = {}
        for item in incoming:
            key = identity(item)
            if key is not None and incoming_identities.get(key) not in (None, item["id"]):
                raise ConflictError("import scoped identity conflict")
            if key is not None:
                incoming_identities[key] = item["id"]

        try:
            with self.engine.begin() as connection:
                existing_rows = [dict(row) for row in connection.execute(select(records)).mappings()]
                existing = {row["id"]: row for row in existing_rows}
                existing_identities = {
                    key: row["id"]
                    for row in existing_rows
                    if (key := identity(row)) is not None
                }
                for item in incoming:
                    current = existing.get(item["id"])
                    if current is not None and current != item:
                        raise ConflictError(f"import conflict for record {item['id']}")
                    key = identity(item)
                    if key is not None and existing_identities.get(key) not in (None, item["id"]):
                        raise ConflictError("import scoped identity conflict")
                for item in incoming:
                    if item["id"] not in existing:
                        connection.execute(records.insert().values(**item))
        except IntegrityError as exc:
            raise ConflictError("import conflict during concurrent write") from exc

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
    "ConflictError",
    "NotFoundError",
]
