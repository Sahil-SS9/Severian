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
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar, cast

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    inspect,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from severian.application.jobrecovery import REQUEUEABLE_JOB_STATUSES, requeue_audit, requeue_reset
from severian.application.purge import (
    EraseOutcome,
    PurgePlan,
    PurgeReport,
    drop_purged_support,
    plan_purge,
    scrub_entity,
    scrub_memory,
    scrub_observation,
)
from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import (
    ERASED,
    SINGLE_VALUED_PREDICATES,
    AuditEvent,
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
    Scope,
    now,
    revision_lineage,
    source_identity,
)
from severian.infrastructure.snapshot import validate_snapshot
from severian.infrastructure.task_notes import TaskNoteStore

T = TypeVar("T")


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 UTC string for column comparison (lexicographic == chronological)."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()


# Bind-parameter chunk for IN-clause bulk ops (ack_outbox_many). PG caps
# at 65535/statement; SQLite at 32766 (3.32+). 5000 is safely under both.
_ACK_CHUNK = 5000

# ``jsonb_set`` path for the supersede write. It MUST be a ``text[]``: passing a
# bare ``VARCHAR`` path (the base-branch ``record_memory_usage`` bug) is what made
# that statement unusable on PostgreSQL.
SUPERSEDED_BY_PATH = sa_text("'{superseded_by_id}'::text[]")



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


class PostgreSQLBackend(TaskNoteStore):
    _records = records

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
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_records_type ON records (record_type)")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_records_tenant ON records (tenant_id)")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_records_collection ON records (collection_id)")
            connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_records_status ON records (status)")

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
                version = connection.execute(sa_text("SELECT version_num FROM alembic_version")).scalar_one_or_none()

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
                f"reset() refused: database is not test-owned (db_name={db_name!r}, SEVERIAN_PG_ALLOW_RESET not set)"
            )
        with self.engine.begin() as connection:
            pg_vectors.drop(connection, checkfirst=True)
            metadata.drop_all(connection)
        self.initialize()
        # The pgvector projection table is owned by PGVectorIndex (its
        # dimension derives from the embedding provider, 256 or 384).
        # reset() drops it here but does NOT recreate it — the owning
        # index recreates it at the correct dimension on its own
        # initialize()/create_all call. Recreating at a hardcoded 256
        # here would clobber a Granite (384-dim) schema.

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
        event_suffix = {"create": "created", "revise": "revised", "rollback": "rolled_back"}[operation]
        event = Event(scope=scope, kind=f"{kind}.{event_suffix}", payload={"id": model.id})
        audit = AuditEvent(scope=scope, action=f"{kind}.{operation}", target_id=model.id, metadata={})
        return event, audit

    @staticmethod
    def _scope_predicates(scope: Scope) -> list[Any]:
        return [getattr(records.c, key) == value for key, value in scope.model_dump().items()]

    @staticmethod
    def _active_observation_only() -> Any:
        """Dedupe predicate: only an ACTIVE observation answers a content re-ingest (M1, MG-2).

        The observation identity lookup used to match a tombstoned (erased) or purged row, so
        re-stating a forgotten fact returned the dead record and silently dropped the new
        statement. A row whose ``superseded_by_id`` is set is not current evidence; the same
        text ingested again is a new observation that derives and becomes findable. Purged
        rows also release their identity key, so this is the second of two guards.
        """
        return records.c.payload["superseded_by_id"].astext.is_(None)

    def _release_stale_observation_identity(
        self, connection: Connection, scope: Scope, identity_key: str
    ) -> Observation | None:
        """Release the identity key a NON-ACTIVE row still holds; return an active holder.

        Mirrors ``SQLiteRepository`` exactly (M1 F2). A store written before M1 erased
        observations the old way: the payload was tombstoned and the identity key was left
        in place. The dedupe lookup is active-only (MG-2), so re-stating that forgotten text
        is new evidence - and then collides on the partial unique index against a row the
        lookup cannot see. Releasing that one key is what lets the restatement be stored.

        Runs on the caller's transaction, under the full five-dimension scope and the
        identity key itself. An ACTIVE holder is returned untouched (a genuine dedupe hit);
        a purged row that still holds a key is corrupt state and fails closed.
        """
        rows = connection.execute(
            select(records.c.id, records.c.payload).where(
                records.c.record_type == "observation",
                *self._scope_predicates(scope),
                records.c.identity_key == identity_key,
            )
        ).mappings().all()
        for row in rows:
            stored = Observation.model_validate(row["payload"])
            if stored.superseded_by_id is None:
                return stored
            if stored.purged:
                raise ConflictError("a purged observation still holds its identity key")
            released = connection.execute(
                update(records)
                .where(
                    records.c.id == row["id"],
                    records.c.record_type == "observation",
                    *self._scope_predicates(scope),
                    records.c.identity_key == identity_key,
                )
                .values(identity_key=None)
            )
            if released.rowcount != 1:
                continue  # another writer released it first: nothing left to undo
            self._insert_audit_in_transaction(
                connection,
                scope,
                "observation.identity_release",
                row["id"],
                {"reason": "a non-active record held the identity key of re-ingested evidence"},
            )
        return None

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

    @staticmethod
    def _insert_audit_in_transaction(
        connection: Connection,
        scope: Scope,
        action: str,
        target_id: str,
        metadata: dict[str, Any],
    ) -> AuditEvent:
        """Append ONE audit row inside the caller's transaction (M2, W-11.5).

        Audit only. ``_insert_events_in_transaction`` must NOT be used for these
        paths: it also appends a pending outbox event (which drives the
        projection) and its ``_event_models`` map knows only create/revise/
        rollback, so a new operation name raises ``KeyError``. Audit rows are
        append-only — never updated, never deleted.
        """
        audit = AuditEvent(scope=scope, action=action, target_id=target_id, metadata=metadata)
        connection.execute(records.insert().values(**PostgreSQLBackend._record_values("audit", audit)))
        return audit

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

    def collection_exists(self, collection_id: str) -> bool:
        """True if a collection with this id exists for ANY tenant."""
        with self.engine.connect() as connection:
            row = connection.execute(
                select(records.c.id).where(
                    records.c.record_type == "collection",
                    records.c.id == collection_id,
                )
            ).scalar_one_or_none()
        return row is not None

    # -- observations ------------------------------------------------------

    def add_observation(self, observation: Observation) -> Observation:
        self._require_scope(observation.scope)
        self._require_collection(observation.scope)
        digest = hashlib.sha256(observation.content.encode()).hexdigest()
        # P0-1: preserve immutable raw evidence. Direct constructions that
        # omit raw fields are backfilled so the stored record always has a
        # raw_hash; the raw pair is never rewritten on revision.
        raw = observation.raw_content or observation.content
        raw_hash = observation.raw_hash or hashlib.sha256(raw.encode()).hexdigest()
        candidate = observation.model_copy(update={"content_hash": digest, "raw_content": raw, "raw_hash": raw_hash})
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
            self._active_observation_only(),
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Observation.model_validate(existing)
        try:
            with self.engine.begin() as connection:
                # F2: release the key a pre-M1 erasure left behind, in this same transaction.
                if (
                    held := self._release_stale_observation_identity(
                        connection, candidate.scope, identity_key
                    )
                ) is not None:
                    return held
                connection.execute(
                    records.insert().values(
                        **self._record_values(
                            "observation", candidate, content_hash=digest, identity_key=identity_key
                        )
                    )
                )
                if (events := self._event_models("observation", candidate)) is not None:
                    event, audit = events
                    connection.execute(
                        records.insert().values(**self._record_values("outbox", event, status="pending"))
                    )
                    connection.execute(records.insert().values(**self._record_values("audit", audit)))
        except IntegrityError:
            with self.engine.connect() as connection:
                raced = connection.execute(query).scalar_one_or_none()
            if raced is None:
                raise ConflictError("observation identity conflict") from None
            return Observation.model_validate(raced)
        return candidate

    def get_observation(self, scope: Scope, observation_id: str) -> Observation:
        self._require_scope(scope)
        return self._get("observation", observation_id, Observation, scope=scope)

    def _prepare_observation_enqueue(
        self, observation: Observation, job_kind: str
    ) -> tuple[Observation, str, Job, str, str]:
        """Pure prep shared by single and batch observation ingest.

        Computes the content digest, identity key, candidate model and
        derive job — identical semantics for both paths so the batch
        never drifts from the canonical contract (zero-debt 3.3).
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
        job = Job(scope=candidate.scope, kind=job_kind, payload=job_payload)
        job_identity = self._identity(job_kind, job_payload)
        return candidate, observation_identity, job, job_identity, digest

    def _stored_pair(
        self, stored: Observation, job_kind: str, also_queue: Sequence[str]
    ) -> tuple[Observation, Job]:
        """The stored observation and its derive job, queueing the job if it is missing.

        Mirrors ``SQLiteRepository._stored_pair``: shared by a re-ingest and by the race
        where another writer commits the same evidence between our lookup and our insert.
        """
        for kind in also_queue:  # the turn is already committed; queueing is idempotent per kind
            self.enqueue_job(stored.scope, kind, {"observation_id": stored.id})
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

    def add_observation_and_enqueue(
        self, observation: Observation, job_kind: str = "derive", *, also_queue: Sequence[str] = ()
    ) -> tuple[Observation, Job]:
        """Atomically insert an observation and enqueue its derive job.

        P0-4: the canonical write and the queue write are ONE
        transaction. Identity semantics match ``add_observation`` +
        ``enqueue_job`` so re-ingests stay idempotent.
        """
        candidate, observation_identity, job, job_identity, digest = self._prepare_observation_enqueue(
            observation, job_kind
        )
        observation_query = select(records.c.payload).where(
            records.c.record_type == "observation",
            *self._scope_predicates(candidate.scope),
            records.c.identity_key == observation_identity,
            self._active_observation_only(),
        )
        with self.engine.connect() as connection:
            existing_obs = connection.execute(observation_query).scalar_one_or_none()
        if existing_obs is not None:
            return self._stored_pair(Observation.model_validate(existing_obs), job_kind, also_queue)

        job = Job(scope=candidate.scope, kind=job_kind, payload={"observation_id": candidate.id})
        job_identity = self._identity(job_kind, job.payload)
        held: Observation | None = None
        try:
            with self.engine.begin() as connection:
                # F2: release the key a pre-M1 erasure left behind, in this same transaction,
                # so the insert below cannot collide with a row the lookup cannot see.
                held = self._release_stale_observation_identity(
                    connection, candidate.scope, observation_identity
                )
                if held is None:
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
                        connection.execute(
                            records.insert().values(**self._record_values("outbox", event, status="pending"))
                        )
                        connection.execute(records.insert().values(**self._record_values("audit", audit)))
                    connection.execute(
                        records.insert().values(
                            **self._record_values("job", job, identity_key=job_identity, status="pending")
                        )
                    )
                    for kind in also_queue:  # same transaction: no saved turn without its extra jobs
                        extra = Job(scope=candidate.scope, kind=kind, payload={"observation_id": candidate.id})
                        connection.execute(
                            records.insert().values(
                                **self._record_values(
                                    "job",
                                    extra,
                                    identity_key=self._identity(kind, extra.payload),
                                    status="pending",
                                )
                            )
                        )
        except IntegrityError:
            # A concurrent writer committed the same evidence first: return its pair.
            with self.engine.connect() as connection:
                raced = connection.execute(observation_query).scalar_one_or_none()
            if raced is None:
                raise ConflictError("observation identity conflict") from None
            held = Observation.model_validate(raced)
        if held is not None:
            return self._stored_pair(held, job_kind, also_queue)
        return candidate, job

    def add_observations_and_enqueue_batch(
        self, observations: list[Observation], job_kind: str = "derive"
    ) -> list[tuple[Observation, Job]]:
        """Batch observation ingest in ONE transaction (zero-debt 3.3).

        Same canonical contract as ``add_observation_and_enqueue``
        (identity dedupe, provenance, event emission, P0-4 atomicity)
        but every new observation, outbox event, audit row and derive
        job commits in a single transaction — never per row. Already
        ingested observations return their stored pair (idempotent).
        """
        prepared = [self._prepare_observation_enqueue(o, job_kind) for o in observations]
        results: list[tuple[Observation, Job]] = []
        fresh: list[tuple[Observation, str, Job, str, str]] = []
        for candidate, observation_identity, job, job_identity, digest in prepared:
            observation_query = select(records.c.payload).where(
                records.c.record_type == "observation",
                *self._scope_predicates(candidate.scope),
                records.c.identity_key == observation_identity,
                self._active_observation_only(),
            )
            with self.engine.connect() as connection:
                existing_obs = connection.execute(observation_query).scalar_one_or_none()
            if existing_obs is None:
                fresh.append((candidate, observation_identity, job, job_identity, digest))
                continue
            # Re-ingest: return the stored observation + its derive job.
            results.append(self._stored_pair(Observation.model_validate(existing_obs), job_kind, ()))

        if not fresh:
            return results
        raced: list[Observation] = []
        try:
            with self.engine.begin() as connection:
                inserted: set[str] = set()
                for candidate, observation_identity, job, job_identity, digest in fresh:
                    # The same evidence twice in one batch is a conflict, not a silent dedupe:
                    # the batch commits as a whole or not at all (P0-4).
                    if observation_identity in inserted:
                        raise ConflictError(
                            "observation identity conflict: the same evidence appears twice in one batch"
                        )
                    # F2: same release as the single path, inside this batch transaction.
                    held = self._release_stale_observation_identity(
                        connection, candidate.scope, observation_identity
                    )
                    if held is not None:
                        raced.append(held)
                        continue
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
                        connection.execute(
                            records.insert().values(
                                **self._record_values("outbox", event, status="pending")
                            )
                        )
                        connection.execute(records.insert().values(**self._record_values("audit", audit)))
                    connection.execute(
                        records.insert().values(
                            **self._record_values("job", job, identity_key=job_identity, status="pending")
                        )
                    )
                    results.append((candidate, job))
                    inserted.add(observation_identity)
        except IntegrityError:
            raise ConflictError("observation identity conflict") from None
        for stored in raced:
            results.append(self._stored_pair(stored, job_kind, ()))
        return results

    def observation_positions(self, scope: Scope) -> dict[str, int]:
        """Insertion positions for every observation in scope.

        Live Hermes turns carry no ``turn`` in source, so message units fall
        back to the observation's place in the conversation. ``created_at``
        first, then PostgreSQL's physical insertion order (``ctid``).
        """
        self._require_scope(scope)
        query = (
            select(records.c.id)
            .where(records.c.record_type == "observation", *self._scope_predicates(scope))
            .order_by(records.c.payload["created_at"].astext, sa_text("ctid"))
        )
        with self.engine.connect() as connection:
            return {
                str(record_id): position
                for position, record_id in enumerate(connection.execute(query).scalars(), 1)
            }

    def list_observations(self, scope: Scope) -> list[Observation]:
        """All observations in scope, including superseded revisions.

        Contract (append-only): the canonical store retains every
        observation as immutable raw evidence (P0-1); a revision marks
        the old head superseded but never removes it. Consumers that
        need active-only semantics (e.g. the derive pass) filter
        ``superseded_by_id`` themselves.
        """
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "observation", *self._scope_predicates(scope))
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [Observation.model_validate(value) for value in connection.execute(query).scalars()]

    def list_pending_observations(self, scope: Scope, *, limit: int) -> list[Observation]:
        """Active observations with NO derived active memory yet.

        Bounded SQL lookup (zero-debt 3.1): a single query answers
        pending work for the scope without scanning every observation
        or counting memories. JSONB containment checks whether any
        active memory in the scope references the observation as a
        source. Oldest-first ordering matches the derive pass contract.
        """
        self._require_scope(scope)
        sql = sa_text(
            """
            WITH covered_obs AS (
                SELECT DISTINCT je AS obs_id
                FROM records m, jsonb_array_elements_text(m.payload->'source_observation_ids') je
                WHERE m.record_type = 'memory'
                  AND m.tenant_id = :tenant_id
                  AND m.profile_id = :profile_id
                  AND m.collection_id = :collection_id
                  AND m.agent_id = :agent_id
                  AND m.session_id = :session_id
                  AND je IS NOT NULL
            )
            SELECT o.payload FROM records o
            WHERE o.record_type = 'observation'
              AND o.tenant_id = :tenant_id
              AND o.profile_id = :profile_id
              AND o.collection_id = :collection_id
              AND o.agent_id = :agent_id
              AND o.session_id = :session_id
              AND o.payload->>'superseded_by_id' IS NULL
              AND o.id::text NOT IN (SELECT obs_id FROM covered_obs)
            ORDER BY o.id
            LIMIT :limit
            """
        )
        params = {**scope.model_dump(), "limit": limit}
        with self.engine.connect() as connection:
            return [
                Observation.model_validate(value)
                for value in connection.execute(sql, params).scalars()
            ]

    def list_observations_history(self, scope: Scope) -> list[Observation]:
        """All observations in scope INCLUDING superseded revisions.

        Explicit alias of :meth:`list_observations` for consumers that
        need the raw evidence trail (temporal probes, rollback). The
        store is append-only: every revision is retained.
        """
        return self.list_observations(scope)

    def update_observation_content(self, observation_id: str, content: str) -> None:
        del observation_id, content
        raise ConflictError("observations are immutable; create a revision")

    def revise_observation(
        self,
        scope: Scope,
        observation_id: str,
        content: str,
        *,
        derive: bool = False,
        tombstone: bool = False,
        also_queue: Sequence[str] = (),
    ) -> Observation:
        if derive and tombstone:
            raise ValueError("an erasure tombstone is never derived")
        if also_queue and tombstone:
            raise ValueError("an erasure tombstone queues no jobs")
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
            superseded_by_id=ERASED if tombstone else None,
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
                .values(payload=changed.model_dump(mode="json"), identity_key=None)
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
            if derive:
                # Same job shape and identity as ingest, in the same transaction:
                # a correction can never commit without its re-derivation queued.
                job_payload = {"observation_id": revision.id}
                job = Job(scope=scope, kind="derive", payload=job_payload)
                connection.execute(
                    records.insert().values(
                        **self._record_values(
                            "job", job, identity_key=self._identity("derive", job_payload), status="pending"
                        )
                    )
                )
            for kind in also_queue:  # same transaction as the revision
                extra = Job(scope=scope, kind=kind, payload={"observation_id": revision.id})
                connection.execute(
                    records.insert().values(
                        **self._record_values(
                            "job", extra, identity_key=self._identity(kind, extra.payload), status="pending"
                        )
                    )
                )
        return revision

    # -- memories ----------------------------------------------------------

    def add_memory(self, memory: Memory) -> Memory:
        return self._write_memory(memory, audit=None)[0]

    def add_memory_with_audit(
        self,
        memory: Memory,
        *,
        action: str,
        metadata_: dict[str, Any],
    ) -> tuple[Memory, AuditEvent]:
        """Create ``memory`` and ONE audit row naming it, in a single transaction.

        Creation is ``add_memory``'s, unchanged: the same source validation, the
        same identity key, the same dedupe against an identical memory in the
        scope and the same ``memory.created`` outbox event, so the projection
        still sees exactly one creation. The extra row is appended by the
        audit-only helper to the SAME transaction, so a claim and its evidence
        cannot be separated by a failure. On an identity hit nothing is inserted
        and the row names the memory that is actually stored.
        """
        stored, row = self._write_memory(memory, audit=(action, dict(metadata_)))
        assert row is not None  # an audit row was requested, so one is always returned
        return stored, row

    def _write_memory(
        self,
        memory: Memory,
        *,
        audit: tuple[str, dict[str, Any]] | None,
    ) -> tuple[Memory, AuditEvent | None]:
        """The one creation path behind ``add_memory`` and ``add_memory_with_audit``.

        Same source-observation validation, same identity key, same dedupe and
        the same creation outbox event either way. ``audit`` asks for ONE extra
        append-only row: written inside the insert's transaction when the memory
        is new, and on its own naming the stored memory when an identical one
        already exists (nothing was created, so nothing else changes).
        """
        self._require_collection(memory.scope)
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
            stored = Memory.model_validate(existing)
            return stored, self._audit_after_identity_hit(memory.scope, stored.id, audit)
        try:
            with self.engine.begin() as connection:
                return memory, self._insert_memory_row(connection, memory, identity_key, audit)
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(query).scalar_one_or_none()
            if existing is None:
                raise ConflictError("memory identity conflict") from None
            stored = Memory.model_validate(existing)
            return stored, self._audit_after_identity_hit(memory.scope, stored.id, audit)

    def _insert_memory_row(
        self,
        connection: Connection,
        memory: Memory,
        identity_key: str,
        audit: tuple[str, dict[str, Any]] | None,
    ) -> AuditEvent | None:
        """Insert the memory, its creation outbox event and optionally one audit row.

        All three statements share the caller's transaction: the memory cannot
        land without the event the projection needs, nor without the audit row
        the caller asked for.
        """
        connection.execute(
            records.insert().values(**self._record_values("memory", memory, identity_key=identity_key))
        )
        if (events := self._event_models("memory", memory)) is not None:
            event, creation_audit = events
            connection.execute(
                records.insert().values(**self._record_values("outbox", event, status="pending"))
            )
            connection.execute(records.insert().values(**self._record_values("audit", creation_audit)))
        if audit is None:
            return None
        action, metadata = audit
        return self._insert_audit_in_transaction(connection, memory.scope, action, memory.id, metadata)

    def _audit_after_identity_hit(
        self,
        scope: Scope,
        target_id: str,
        audit: tuple[str, dict[str, Any]] | None,
    ) -> AuditEvent | None:
        """Write the requested audit row on its own when no memory insert happened."""
        if audit is None:
            return None
        action, metadata = audit
        with self.engine.begin() as connection:
            return self._insert_audit_in_transaction(connection, scope, action, target_id, metadata)

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
            existing_by_identity: dict[tuple[str, str], Memory] = {}
            rows = (
                connection.execute(
                    select(records.c.payload).where(
                        records.c.record_type == "memory",
                        records.c.identity_key.in_(identities),
                    )
                )
                .scalars()
                .all()
            )
            for value in rows:
                memory = Memory.model_validate(value)
                key = self._identity(
                    memory.kind,
                    memory.content,
                    memory.source_observation_ids,
                    memory.model_id,
                    memory.prompt_version,
                )
                # DATA-06: key by (scope, identity) — a memory with the
                # same identity in ANOTHER scope must NOT satisfy this
                # batch insert (the single-insert path already scopes;
                # the batch path must agree). Without this, a cross-scope
                # match would suppress a new insert or return the wrong
                # memory.
                scope_key = memory.scope.model_dump_json()
                existing_by_identity[(scope_key, key)] = memory
            results: list[Memory] = []
            for memory, identity_key in zip(memories, identities, strict=True):
                scope_key = memory.scope.model_dump_json()
                if (scope_key, identity_key) in existing_by_identity:
                    results.append(existing_by_identity[(scope_key, identity_key)])
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

    def publish_derivation(
        self,
        scope: Scope,
        *,
        entities: list[tuple[str, str]],
        relations: list[tuple[str, str, str]],
        memories: list[Memory],
        source_observation_id: str | None = None,
        relation_valid_from: datetime | None = None,
    ) -> list[Memory]:
        """Atomically publish graph extraction + derived memories (DATA-04).

        The graph entities/relations AND the candidate memories land in
        ONE transaction: a failure anywhere aborts everything, so the
        graph can never be orphaned from the memory it came from (or
        vice versa). Entity/relation upserts are identity-deduped exactly
        like ``add_entity``/``add_relation``; memory insert matches
        ``add_memories_batch`` semantics. ``source_observation_id`` is
        recorded as relation lineage (``Relation.supported_by``).
        """
        self._require_scope(scope)
        self._require_collection(scope)
        if not memories and not entities and not relations:
            return []
        # Validate memory source references BEFORE the transaction.
        for memory in memories:
            for source_id in memory.source_observation_ids:
                source = self.get_observation(memory.scope, source_id)
                if source.scope != memory.scope:
                    raise ConflictError("source observation scope does not match memory scope")

        memory_identities = [
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
            # -- entities (identity-deduped, same as add_entity) --
            entity_ids: dict[str, str] = {}
            for kind, name in entities:
                identity_key = self._identity(kind, name.casefold())
                row = connection.execute(
                    select(records.c.payload).where(
                        records.c.record_type == "entity",
                        *self._scope_predicates(scope),
                        records.c.identity_key == identity_key,
                    )
                ).scalar_one_or_none()
                if row is not None:
                    entity_ids[name.casefold()] = Entity.model_validate(row).id
                    continue
                entity = Entity(scope=scope, kind=kind, name=name)
                connection.execute(
                    records.insert().values(
                        **self._record_values("entity", entity, identity_key=identity_key)
                    )
                )
                if (events := self._event_models("entity", entity)) is not None:
                    event, audit = events
                    connection.execute(
                        records.insert().values(**self._record_values("outbox", event, status="pending"))
                    )
                    connection.execute(records.insert().values(**self._record_values("audit", audit)))
                entity_ids[name.casefold()] = entity.id
            # -- relations (only when both endpoints were upserted) --
            for subject, predicate, obj in relations:
                subject_id = entity_ids.get(subject.casefold())
                object_id = entity_ids.get(obj.casefold())
                if subject_id is None or object_id is None:
                    continue
                relation = Relation(
                    scope=scope,
                    subject_id=subject_id,
                    predicate=predicate,
                    object_id=object_id,
                    valid_from=relation_valid_from,
                    source_observation_ids=[source_observation_id] if source_observation_id else [],
                )
                identity_key = self._identity(
                    "relation", relation.subject_id, relation.predicate, relation.object_id
                )
                existing = connection.execute(
                    select(records.c.id, records.c.payload).where(
                        records.c.record_type == "relation",
                        *self._scope_predicates(scope),
                        records.c.identity_key == identity_key,
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    current = Relation.model_validate(existing["payload"])
                    supported = current.supported_by(source_observation_id) or current
                    reopened = supported.reopened() if predicate in SINGLE_VALUED_PREDICATES else None
                    updated = reopened or (supported if supported is not current else None)
                    if updated is not None:
                        connection.execute(
                            update(records)
                            .where(records.c.id == existing["id"])
                            .values(payload=updated.model_dump(mode="json"))
                        )
                        self._insert_audit_in_transaction(
                            connection,
                            scope,
                            "relation.reopen" if reopened is not None else "relation.support",
                            existing["id"],
                            {
                                "source_observation_id": source_observation_id,
                                "supported": supported is not current,
                            },
                        )
                    self._end_replaced_relations(connection, scope, updated or current, event_at=relation_valid_from)
                    continue
                connection.execute(
                    records.insert().values(
                        **self._record_values("relation", relation, identity_key=identity_key)
                    )
                )
                if (events := self._event_models("relation", relation)) is not None:
                    event, audit = events
                    connection.execute(
                        records.insert().values(**self._record_values("outbox", event, status="pending"))
                    )
                    connection.execute(records.insert().values(**self._record_values("audit", audit)))
                self._end_replaced_relations(connection, scope, relation, event_at=relation_valid_from)
            # -- memories (same as add_memories_batch) --
            existing_rows = connection.execute(
                select(records.c.payload).where(
                    records.c.record_type == "memory",
                    records.c.identity_key.in_(memory_identities),
                )
            ).scalars().all()
            existing_by_identity: dict[tuple[str, str], Memory] = {}
            for value in existing_rows:
                memory = Memory.model_validate(value)
                key = self._identity(
                    memory.kind,
                    memory.content,
                    memory.source_observation_ids,
                    memory.model_id,
                    memory.prompt_version,
                )
                scope_key = memory.scope.model_dump_json()
                existing_by_identity[(scope_key, key)] = memory
            results: list[Memory] = []
            for memory, identity_key in zip(memories, memory_identities, strict=True):
                scope_key = memory.scope.model_dump_json()
                if (scope_key, identity_key) in existing_by_identity:
                    results.append(existing_by_identity[(scope_key, identity_key)])
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

    def record_memory_usage(self, scope: Scope, memory_id: str) -> Memory:
        """Increment a memory's usage_count by one (retrieval hit).

        Atomic increment (DATA-07): the update runs in one statement
        against the CURRENT payload — it never rewrites a stale read-back
        copy. A concurrent supersede can therefore not be lost to a
        usage update that overwrote the whole payload with an old value.
        """
        self._require_scope(scope)
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(
                    records.c.record_type == "memory",
                    records.c.id == memory_id,
                    *self._scope_predicates(scope),
                    records.c.payload["superseded_by_id"].astext.is_(None),
                )
                .values(
                    payload=func.jsonb_set(
                        records.c.payload,
                        "{usage_count}",
                        (records.c.payload["usage_count"].astext.cast(Integer) + 1),
                    )
                )
            )
            if result.rowcount == 0:
                # No active memory in this scope with this id: it is either
                # superseded (usage on stale heads is not recorded) or does
                # not exist. Return the (possibly superseded) record as-is
                # for the caller, matching the pre-fix no-op contract.
                return self.get_memory(scope, memory_id)
        return self.get_memory(scope, memory_id)

    def close_memory(self, scope: Scope, memory_id: str, *, valid_until: datetime, replaced_by_id: str) -> bool:
        """Mark one active, still-open memory as replaced; it stays as history (see the port).

        One conditional UPDATE against the CURRENT payload: a concurrent supersede, erase or earlier
        close makes it match nothing (returns False), and it never rewrites a stale read-back copy.
        The ``memory.close`` audit row lands in that same transaction, so a committed close always
        has its audit and a rolled-back one has neither.
        """
        self._require_scope(scope)
        change = {"valid_until": valid_until.isoformat(), "replaced_by_id": replaced_by_id}
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(
                    records.c.record_type == "memory",
                    records.c.id == memory_id,
                    *self._scope_predicates(scope),
                    records.c.payload["superseded_by_id"].astext.is_(None),
                    records.c.payload["valid_until"].astext.is_(None),
                )
                .values(payload=records.c.payload.op("||")(literal(change, type_=JSONB)))
            )
            if result.rowcount == 1:
                self._insert_audit_in_transaction(
                    connection,
                    scope,
                    "memory.close",
                    memory_id,
                    {"valid_until": valid_until, "replaced_by_id": replaced_by_id},
                )
        return result.rowcount == 1

    def count_memories(self, scope: Scope) -> int:
        """Count ACTIVE memories in scope (cheap COUNT, no payload load)."""
        self._require_scope(scope)
        query = (
            select(func.count())
            .select_from(records)
            .where(
                records.c.record_type == "memory",
                *self._scope_predicates(scope),
                records.c.payload["superseded_by_id"].astext.is_(None),
            )
        )
        with self.engine.connect() as connection:
            return int(connection.execute(query).scalar_one())

    def list_session_observations(self, scope: Scope, session_ids: list[str]) -> list[Observation]:
        """Observations in scope whose ``source.session_id`` is one of ``session_ids`` (neighbour lookup)."""
        self._require_scope(scope)
        wanted = sorted({str(value) for value in session_ids})
        if not wanted:
            return []
        query = (
            select(records.c.payload)
            .where(
                records.c.record_type == "observation",
                *self._scope_predicates(scope),
                records.c.payload["source"]["session_id"].astext.in_(wanted),
            )
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [Observation.model_validate(value) for value in connection.execute(query).scalars()]

    def list_memories_for_observations(self, scope: Scope, observation_ids: list[str]) -> list[Memory]:
        """ACTIVE memories in scope derived from any of ``observation_ids`` (neighbour lookup)."""
        self._require_scope(scope)
        wanted = sorted(set(observation_ids))
        if not wanted:
            return []
        source = (
            func.jsonb_array_elements_text(records.c.payload["source_observation_ids"])
            .table_valued("value")
            .alias("src")
        )
        query = (
            select(records.c.payload)
            .where(
                records.c.record_type == "memory",
                *self._scope_predicates(scope),
                records.c.payload["superseded_by_id"].astext.is_(None),
                select(source.c.value).where(source.c.value.in_(wanted)).exists(),
            )
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [Memory.model_validate(value) for value in connection.execute(query).scalars()]

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

    def list_trigger_memories(self, scope: Scope) -> list[Memory]:
        """ACTIVE memories that carry a trigger (Step 4), oldest first; reads only those rows."""
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(
                records.c.record_type == "memory",
                *self._scope_predicates(scope),
                records.c.payload["superseded_by_id"].astext.is_(None),
                records.c.payload["trigger"].astext.is_not(None),
            )
            .order_by(records.c.payload["created_at"].astext, records.c.id)
        )
        with self.engine.connect() as connection:
            return [Memory.model_validate(value) for value in connection.execute(query).scalars()]

    def list_memory_scopes(self) -> list[Scope]:
        """List every active-memory scope for operator projection rebuilds."""
        query = (
            select(
                records.c.tenant_id,
                records.c.profile_id,
                records.c.collection_id,
                records.c.agent_id,
                records.c.session_id,
            )
            .where(
                records.c.record_type == "memory",
                records.c.payload["superseded_by_id"].astext.is_(None),
            )
            .distinct()
            .order_by(
                records.c.tenant_id,
                records.c.profile_id,
                records.c.collection_id,
                records.c.agent_id,
                records.c.session_id,
            )
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query).mappings().all()
        scopes: list[Scope] = []
        for row in rows:
            values = {key: row[key] for key in ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")}
            if any(not isinstance(value, str) or not value for value in values.values()):
                raise ValueError("active memory has an invalid persisted scope")
            scopes.append(Scope(**values))
        return scopes

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
        """Return every revision in the memory's lineage, oldest first (``revision_lineage``)."""
        self._require_scope(scope)
        chain: dict[str, Memory] = {}
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(records.c.payload).where(records.c.record_type == "memory", *self._scope_predicates(scope))
            ).scalars()
            for value in rows:
                memory = Memory.model_validate(value)
                chain[memory.id] = memory
        if memory_id not in chain:
            raise NotFoundError("memory not found")
        return revision_lineage(chain, memory_id)

    def retract_relations_for_source(self, scope: Scope, observation_id: str) -> int:
        """Drop ``observation_id`` from relation lineage; retract relations left unsupported.

        A relation that keeps another support is audited as ``relation.unsupport``;
        one that loses its last support is audited as ``relation.retract``. One row
        per affected relation, in the same transaction.
        """
        self._require_scope(scope)
        with self.engine.begin() as connection:
            return self._retract_relations_in_transaction(connection, scope, observation_id)

    def _retract_relations_in_transaction(self, connection: Connection, scope: Scope, observation_id: str) -> int:
        """The body of :meth:`retract_relations_for_source`, on the caller's transaction.

        Shared with the atomic erasure cascade (M1), so both paths write the same rows,
        the same audit entries and the same retraction rule.
        """
        sql = sa_text(
            """
            SELECT r.id, r.payload FROM records r
            WHERE r.record_type = 'relation'
              AND r.tenant_id = :tenant_id
              AND r.profile_id = :profile_id
              AND r.collection_id = :collection_id
              AND r.agent_id = :agent_id
              AND r.session_id = :session_id
              AND EXISTS (
                SELECT 1 FROM jsonb_array_elements_text(r.payload->'source_observation_ids') je
                WHERE je = :observation_id
              )
            """
        )
        stamp = now()
        retracted = 0
        rows = connection.execute(sql, {**scope.model_dump(), "observation_id": observation_id}).mappings().all()
        for row in rows:
            relation = Relation.model_validate(row["payload"])
            changed = relation.unsupported_by(observation_id, at=stamp)
            if changed is None:
                continue
            became_retracted = relation.retracted_at is None and changed.retracted_at is not None
            if became_retracted:
                retracted += 1
            connection.execute(
                update(records).where(records.c.id == row["id"]).values(payload=changed.model_dump(mode="json"))
            )
            self._insert_audit_in_transaction(
                connection,
                scope,
                "relation.retract" if became_retracted else "relation.unsupport",
                row["id"],
                {"observation_id": observation_id, "retracted": became_retracted},
            )
        return retracted

    def _end_replaced_relations(
        self, connection: Connection, scope: Scope, newer: Relation, *, event_at: datetime | None = None
    ) -> None:
        """End older open values of a single-valued predicate once ``newer`` states a new one.

        ``event_at`` is the newer statement's event instant (S3, switch on): the older value ends
        there rather than at the wall clock, and a value that started after that instant is left
        open — an older statement is history, not a replacement.
        """
        if newer.predicate not in SINGLE_VALUED_PREDICATES or newer.valid_to is not None:
            return
        sql = sa_text(
            """
            SELECT r.id, r.payload FROM records r
            WHERE r.record_type = 'relation'
              AND r.tenant_id = :tenant_id
              AND r.profile_id = :profile_id
              AND r.collection_id = :collection_id
              AND r.agent_id = :agent_id
              AND r.session_id = :session_id
              AND r.payload->>'subject_id' = :subject_id
              AND r.payload->>'predicate' = :predicate
              AND r.id != :newer_id
            """
        )
        stamp = now() if event_at is None else event_at
        rows = connection.execute(
            sql,
            {**scope.model_dump(), "subject_id": newer.subject_id, "predicate": newer.predicate, "newer_id": newer.id},
        ).mappings().all()
        for row in rows:
            ended = Relation.model_validate(row["payload"]).replaced_by(
                newer, at=stamp, from_event_time=event_at is not None
            )
            if ended is not None:
                connection.execute(
                    update(records).where(records.c.id == row["id"]).values(payload=ended.model_dump(mode="json"))
                )
                self._insert_audit_in_transaction(
                    connection,
                    scope,
                    "relation.end",
                    row["id"],
                    {"replaced_by_id": newer.id, "valid_to": ended.valid_to},
                )

    def supersede_memories_for_source(self, scope: Scope, observation_id: str) -> int:
        """Mark active memories derived from an observation as superseded (P0-5).

        One ``memory.source_superseded`` audit row per affected memory, in the same
        transaction.
        """
        self._require_scope(scope)
        superseded = 0
        with self.engine.begin() as connection:
            rows = (
                connection.execute(
                    select(records.c.id, records.c.payload).where(
                        records.c.record_type == "memory",
                        *self._scope_predicates(scope),
                        records.c.payload["superseded_by_id"].astext.is_(None),
                    )
                )
                .mappings()
                .all()
            )
            for row in rows:
                memory = Memory.model_validate(row["payload"])
                if observation_id not in memory.source_observation_ids:
                    continue
                marker = f"source-rev:{observation_id}"
                connection.execute(
                    update(records)
                    .where(records.c.id == row["id"])
                    .values(
                        payload=memory.model_copy(update={"superseded_by_id": marker}).model_dump(mode="json")
                    )
                )
                self._insert_audit_in_transaction(
                    connection,
                    scope,
                    "memory.source_superseded",
                    row["id"],
                    {"observation_id": observation_id, "superseded_by_id": marker},
                )
                superseded += 1
        return superseded

    # -- M1: atomic erasure and purge -------------------------------------

    def _load_scope_rows(self, connection: Connection, scope: Scope, kind: str, model: type[T]) -> list[T]:
        """Every row of ``kind`` in this scope, parsed, on the caller's transaction."""
        rows = connection.execute(
            select(records.c.payload).where(records.c.record_type == kind, *self._scope_predicates(scope))
        ).scalars()
        return [model.model_validate(value) for value in rows]  # type: ignore[attr-defined]

    def _get_in_transaction(
        self, connection: Connection, kind: str, record_id: str, model: type[T], *, scope: Scope
    ) -> T:
        """One scoped record, read on the caller's transaction (never a stale outside read)."""
        row = connection.execute(
            select(records.c.payload).where(
                records.c.id == record_id,
                records.c.record_type == kind,
                *self._scope_predicates(scope),
            )
        ).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"{kind} not found")
        return model.model_validate(row)  # type: ignore[attr-defined,no-any-return]

    def erase_observation_cascade(
        self, scope: Scope, observation_id: str, *, reason: str = "erased"
    ) -> EraseOutcome:
        """Erase an observation and everything derived from it in ONE transaction (MG-3).

        The four transactions the old service path used (tombstone, per-memory tombstone,
        supersede link, relation retraction) become one. The outbox and audit rows the old
        path wrote are reproduced exactly, plus one ``observation.erase`` summary; nothing
        is written if any step fails, so a crash can no longer leave a derived memory active
        under an erased source.
        """
        self._require_scope(scope)
        with self.engine.begin() as connection:
            original = self._get_in_transaction(connection, "observation", observation_id, Observation, scope=scope)
            if original.superseded_by_id is not None:
                raise ConflictError("observation is already superseded")
            marker = f"[{reason}]"
            digest = hashlib.sha256(marker.encode()).hexdigest()
            tombstone = Observation(
                scope=scope,
                content=marker,
                content_hash=digest,
                source=original.source,
                revision_of_id=original.id,
                superseded_by_id=ERASED,
            )
            changed = original.model_copy(update={"superseded_by_id": tombstone.id})
            result = connection.execute(
                update(records)
                .where(
                    records.c.id == original.id,
                    records.c.record_type == "observation",
                    *self._scope_predicates(scope),
                    records.c.payload["superseded_by_id"].astext.is_(None),
                )
                .values(payload=changed.model_dump(mode="json"), identity_key=None)
            )
            if result.rowcount != 1:
                raise ConflictError("observation erasure conflict")
            connection.execute(
                records.insert().values(
                    **self._record_values(
                        "observation",
                        tombstone,
                        content_hash=digest,
                        identity_key=self._identity("revision", original.id, tombstone.id),
                    )
                )
            )
            # MG-2: the erased row also releases its content identity key (on the UPDATE
            # above), so the same text can be ingested again as NEW evidence instead of
            # colliding with a row that is no longer current evidence.
            self._insert_events_in_transaction(connection, "observation", tombstone, operation="revise")
            erased_memory_ids: list[str] = []
            for memory in self._load_scope_rows(connection, scope, "memory", Memory):
                if memory.superseded_by_id is not None or observation_id not in memory.source_observation_ids:
                    continue
                erased_memory_ids.append(
                    self._tombstone_memory_in_transaction(connection, scope, memory, reason=reason, marker=marker)
                )
            retracted = self._retract_relations_in_transaction(connection, scope, observation_id)
            self._insert_audit_in_transaction(
                connection,
                scope,
                "observation.erase",
                observation_id,
                {
                    "tombstone_id": tombstone.id,
                    "memories_erased": len(erased_memory_ids),
                    "relations_retracted": retracted,
                },
            )
        return EraseOutcome(
            observation_id=observation_id,
            tombstone_id=tombstone.id,
            memory_ids=tuple(erased_memory_ids),
            relations_retracted=retracted,
        )

    def _tombstone_memory_in_transaction(
        self, connection: Connection, scope: Scope, memory: Memory, *, reason: str, marker: str
    ) -> str:
        """Tombstone one memory on the caller's transaction; returns the memory id erased.

        Same shape as ``erase_memory`` in the service: a revision whose own
        ``superseded_by_id`` is the ``:erased`` sentinel (never active, never projected,
        kept for audit), then the original is superseded by it. The tombstone's identity
        is deduped exactly like ``add_memory`` so two memories derived from one source
        cannot collide on the unique index.
        """
        tombstone = Memory(
            scope=scope,
            kind=memory.kind,
            content=marker,
            source_observation_ids=memory.source_observation_ids,
            model_id=memory.model_id,
            prompt_version=f"{memory.prompt_version}:erased",
            confidence=0.0,
            revision_of_id=memory.id,
            superseded_by_id=ERASED,
        )
        identity_key = self._identity(
            tombstone.kind,
            tombstone.content,
            tombstone.source_observation_ids,
            tombstone.model_id,
            tombstone.prompt_version,
        )
        existing = connection.execute(
            select(records.c.id).where(
                records.c.record_type == "memory",
                *self._scope_predicates(scope),
                records.c.identity_key == identity_key,
            )
        ).scalar_one_or_none()
        if existing is None:
            connection.execute(
                records.insert().values(**self._record_values("memory", tombstone, identity_key=identity_key))
            )
            if (events := self._event_models("memory", tombstone)) is not None:
                event, audit = events
                connection.execute(
                    records.insert().values(**self._record_values("outbox", event, status="pending"))
                )
                connection.execute(records.insert().values(**self._record_values("audit", audit)))
            tombstone_id = tombstone.id
        else:
            tombstone_id = str(existing)
        connection.execute(
            update(records)
            .where(
                records.c.id == memory.id,
                records.c.record_type == "memory",
                *self._scope_predicates(scope),
                records.c.payload["superseded_by_id"].astext.is_(None),
            )
            .values(
                payload=func.jsonb_set(
                    records.c.payload,
                    SUPERSEDED_BY_PATH,
                    func.to_jsonb(literal(tombstone_id, type_=String)),
                )
            )
        )
        self._insert_audit_in_transaction(
            connection, scope, "memory.supersede", memory.id, {"replacement_id": tombstone_id}
        )
        return memory.id

    def _scope_plan(self, connection: Connection, scope: Scope, observation_id: str) -> PurgePlan:
        """The shared purge plan, computed from rows read inside the caller's transaction."""
        return plan_purge(
            observation_id=observation_id,
            observations=self._load_scope_rows(connection, scope, "observation", Observation),
            memories=self._load_scope_rows(connection, scope, "memory", Memory),
            relations=self._load_scope_rows(connection, scope, "relation", Relation),
            entities=self._load_scope_rows(connection, scope, "entity", Entity),
        )

    def plan_purge(self, scope: Scope, observation_id: str) -> PurgePlan:
        """Read-only plan for a purge: ids and counts, no writes, no content."""
        self._require_scope(scope)
        with self.engine.connect() as connection:
            return self._scope_plan(connection, scope, observation_id)

    def purge_observation(self, scope: Scope, observation_id: str) -> PurgeReport:
        """Scrub an observation chain, its derived memories, relations and entities in ONE transaction.

        PostgreSQL keeps the old row versions on disk as dead tuples until a ``VACUUM``
        (and only ``VACUUM FULL`` rewrites the heap). This method therefore scrubs the
        current row versions and says so; it does not, and cannot, promise that the dead
        tuples are gone. The CLI prints the operator command and never runs it.
        """
        self._require_scope(scope)
        with self.engine.begin() as connection:
            plan = self._scope_plan(connection, scope, observation_id)
            chain = set(plan.observation_ids)
            observations = 0
            memories = 0
            relations = 0
            retracted = 0
            entities = 0
            audit_rows = 0
            stamp = now()

            for observation_row in self._load_scope_rows(connection, scope, "observation", Observation):
                if observation_row.id not in chain:
                    continue
                scrubbed = scrub_observation(observation_row)
                if scrubbed == observation_row:
                    continue  # already purged: nothing to write, nothing to audit
                connection.execute(
                    update(records)
                    .where(records.c.id == observation_row.id, records.c.record_type == "observation")
                    .values(
                        payload=scrubbed.model_dump(mode="json"),
                        content_hash=scrubbed.content_hash,
                        identity_key=None,
                    )
                )
                self._insert_audit_in_transaction(
                    connection,
                    scope,
                    "observation.purge",
                    observation_row.id,
                    {"chain_size": len(plan.observation_ids), "purged": True},
                )
                observations += 1
                audit_rows += 1

            for memory_row in self._load_scope_rows(connection, scope, "memory", Memory):
                if memory_row.id not in plan.memory_ids:
                    continue
                scrubbed_memory = scrub_memory(memory_row)
                if scrubbed_memory == memory_row:
                    continue
                connection.execute(
                    update(records)
                    .where(records.c.id == memory_row.id, records.c.record_type == "memory")
                    .values(payload=scrubbed_memory.model_dump(mode="json"), identity_key=None)
                )
                self._insert_audit_in_transaction(
                    connection, scope, "memory.purge", memory_row.id, {"chain_size": len(plan.observation_ids)}
                )
                memories += 1
                audit_rows += 1

            for relation_row in self._load_scope_rows(connection, scope, "relation", Relation):
                if relation_row.id not in plan.relation_ids:
                    continue
                changed = drop_purged_support(relation_row, purged_ids=chain, at=stamp)
                if changed == relation_row:
                    continue
                connection.execute(
                    update(records)
                    .where(records.c.id == relation_row.id, records.c.record_type == "relation")
                    .values(payload=changed.model_dump(mode="json"))
                )
                became_retracted = relation_row.retracted_at is None and changed.retracted_at is not None
                retracted += int(became_retracted)
                self._insert_audit_in_transaction(
                    connection,
                    scope,
                    "relation.purge",
                    relation_row.id,
                    {
                        "purged_sources": len(relation_row.source_observation_ids)
                        - len(changed.source_observation_ids),
                        "retracted": became_retracted,
                    },
                )
                relations += 1
                audit_rows += 1

            for entity_row in self._load_scope_rows(connection, scope, "entity", Entity):
                if entity_row.id not in plan.entity_ids:
                    continue
                scrubbed_entity = scrub_entity(entity_row)
                if scrubbed_entity == entity_row:
                    continue
                connection.execute(
                    update(records)
                    .where(records.c.id == entity_row.id, records.c.record_type == "entity")
                    .values(payload=scrubbed_entity.model_dump(mode="json"), identity_key=None)
                )
                self._insert_audit_in_transaction(
                    connection, scope, "entity.purge", entity_row.id, {"purged": True}
                )
                entities += 1
                audit_rows += 1

            cancelled = self._cancel_jobs_for_in_transaction(connection, scope, chain)

        return PurgeReport(
            plan=plan,
            observations=observations,
            memories=memories,
            relations=relations,
            relations_retracted=retracted,
            entities=entities,
            audit_rows=audit_rows,
            cancelled_jobs=cancelled,
        )

    def _cancel_jobs_for_in_transaction(self, connection: Connection, scope: Scope, chain: set[str]) -> int:
        """Cancel pending jobs that name a purged observation; refuse while one is running.

        A queued derive job would otherwise run after the purge and publish a memory from
        the removal marker. A RUNNING job for the chain is the same hazard one step later,
        so it refuses the purge exactly as ``cancel_pending_jobs`` does: stop the worker,
        then retry.
        """
        rows = connection.execute(
            select(records.c.id, records.c.status, records.c.payload).where(
                records.c.record_type == "job",
                *self._scope_predicates(scope),
                records.c.status.in_(("pending", "running")),
                records.c.payload["payload"]["observation_id"].astext.in_(sorted(chain)),
            )
        ).mappings().all()
        if any(row["status"] == "running" for row in rows):
            raise ConflictError("a derivation job for this observation is running; stop the worker first")
        cancelled = 0
        for row in rows:
            job = Job.model_validate(row["payload"]).model_copy(
                update={"status": "cancelled", "last_error": "cancelled by purge"}
            )
            result = connection.execute(
                update(records)
                .where(records.c.id == row["id"], records.c.status == "pending")
                .values(status="cancelled", payload=job.model_dump(mode="json"))
            )
            cancelled += int(result.rowcount)
        return cancelled

    def supersede_memory(self, scope: Scope, memory_id: str, replacement_id: str) -> Memory:
        """Mark one active memory as superseded by a replacement head.

        Used by the dreaming/consolidation pass to retire duplicate heads
        in favour of a survivor. Non-destructive: the memory remains in
        history (P0-1); active reads and projection rebuilds drop it.

        ONE conditional UPDATE of the CURRENT payload, guarded on active state and
        the full scope (MG-4/DATA-07): a concurrent close, erase or supersede is
        never lost to a stale read-back copy. A row that does not match — absent,
        foreign scope, erased or already superseded — is refused with
        ``ConflictError``; there is no diagnostic read that could tell them apart
        (owner rule 20/11). The ``memory.supersede`` audit row lands in the same
        transaction.
        """
        self._require_scope(scope)
        if memory_id == replacement_id:
            raise ConflictError("cannot supersede a memory by itself")
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(
                    records.c.record_type == "memory",
                    records.c.id == memory_id,
                    *self._scope_predicates(scope),
                    records.c.payload["superseded_by_id"].astext.is_(None),
                )
                .values(
                    payload=func.jsonb_set(
                        records.c.payload,
                        SUPERSEDED_BY_PATH,
                        func.to_jsonb(literal(replacement_id, type_=String)),
                    )
                )
            )
            if result.rowcount == 1:
                self._insert_audit_in_transaction(
                    connection,
                    scope,
                    "memory.supersede",
                    memory_id,
                    {"replacement_id": replacement_id},
                )
        if result.rowcount != 1:
            # ONE conditional UPDATE, so ONE outcome class (owner rule 20/11). A
            # row that did not match is not an active memory in this scope:
            # absent, foreign scope, erased or already superseded. There is no
            # diagnostic read to classify it — the read would be a second,
            # non-atomic source of truth and could only leak the existence of a
            # foreign-scope row.
            raise ConflictError("memory is not an active memory in this scope")
        return self.get_memory(scope, memory_id)

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
                # Metadata derived from the OLD text must not stay attached to the corrected text.
                "occurred_at": None,
                "occurred_until": None,
                "cues": [],
                "trigger": None,
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

    def rollback_memory(self, scope: Scope, revision_id: str) -> Memory:
        """Restore a historical revision as the active memory head (A-2)."""
        target = self.get_memory(scope, revision_id)
        chain_root = target.revision_of_id or target.id
        current = self.get_memory(scope, chain_root)
        visited: set[str] = set()
        while current.superseded_by_id is not None and current.superseded_by_id not in visited:
            visited.add(current.superseded_by_id)
            current = self.get_memory(scope, current.superseded_by_id)
        if current.id == target.id:
            raise ConflictError("revision is already the active head")
        changed_current = current.model_copy(update={"superseded_by_id": target.id})
        restored = target.model_copy(update={"superseded_by_id": None})
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(
                    records.c.id == current.id,
                    records.c.record_type == "memory",
                    *self._scope_predicates(scope),
                )
                .values(payload=changed_current.model_dump(mode="json"))
            )
            if result.rowcount != 1:
                raise ConflictError("memory rollback conflict")
            connection.execute(
                update(records)
                .where(
                    records.c.id == target.id,
                    records.c.record_type == "memory",
                    *self._scope_predicates(scope),
                )
                .values(payload=restored.model_dump(mode="json"))
            )
            self._insert_events_in_transaction(connection, "memory", restored, operation="rollback")
        return self.get_memory(scope, target.id)

    # -- entities and relations --------------------------------------------

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity:
        self._require_scope(scope)
        self._require_collection(scope)
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
        self._require_collection(scope)
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

    def list_entities(self, scope: Scope) -> list[Entity]:
        """All entities in scope, ordered deterministically by (kind, name)."""
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "entity", *self._scope_predicates(scope))
            .order_by(
                records.c.payload["kind"].astext,
                records.c.payload["name"].astext,
                records.c.id,
            )
        )
        with self.engine.connect() as connection:
            return [Entity.model_validate(value) for value in connection.execute(query).scalars()]

    def get_relation(self, scope: Scope, relation_id: str) -> Relation:
        self._require_scope(scope)
        return self._get("relation", relation_id, Relation, scope=scope)

    def list_relations(self, scope: Scope) -> list[Relation]:
        """All relations in scope, ordered deterministically by (subject, predicate, object)."""
        self._require_scope(scope)
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "relation", *self._scope_predicates(scope))
            .order_by(
                records.c.payload["subject_id"].astext,
                records.c.payload["predicate"].astext,
                records.c.payload["object_id"].astext,
                records.c.id,
            )
        )
        with self.engine.connect() as connection:
            return [Relation.model_validate(value) for value in connection.execute(query).scalars()]

    # -- derivation jobs ---------------------------------------------------

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job:
        self._require_scope(scope)
        self._require_collection(scope)
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

    def list_profile_scopes(self, tenant_id: str, profile_id: str) -> list[Scope]:
        """Every scope (collection, agent, session) holding turns, memories or jobs for one profile."""
        columns = (records.c.collection_id, records.c.agent_id, records.c.session_id)
        query = (
            select(*columns)
            .where(
                records.c.tenant_id == tenant_id,
                records.c.profile_id == profile_id,
                records.c.record_type.in_(("observation", "memory", "job")),
            )
            .distinct()
            .order_by(*columns)
        )
        with self.engine.connect() as connection:
            rows = connection.execute(query).all()
        return [
            Scope(tenant_id=tenant_id, profile_id=profile_id, collection_id=c, agent_id=a, session_id=s)
            for c, a, s in rows
        ]

    def cancel_pending_jobs(self, scope: Scope, kind: str) -> int:
        """Cancel ``scope``'s pending jobs of ``kind`` (status ``cancelled``: never claimed again).

        Refuses with ``ConflictError`` while one of them is running, because its worker could still
        publish results after the cancel; stop the worker (or let it finish) and retry.
        """
        self._require_scope(scope)
        query = select(records.c.id, records.c.status, records.c.payload).where(
            records.c.record_type == "job",
            *self._scope_predicates(scope),
            records.c.payload["kind"].astext == kind,
            records.c.status.in_(("pending", "running")),
        )
        with self.engine.begin() as connection:
            rows = connection.execute(query).mappings().all()
            if any(row["status"] == "running" for row in rows):
                raise ConflictError(f"a {kind} job is running in this scope; stop the worker first")
            for row in rows:
                job = Job.model_validate(row["payload"]).model_copy(
                    update={"status": "cancelled", "last_error": "cancelled by rollback"}
                )
                connection.execute(
                    update(records)
                    .where(records.c.id == row["id"], records.c.status == "pending")
                    .values(status="cancelled", payload=job.model_dump(mode="json"))
                )
        return len(rows)

    def claim_job(
        self,
        worker_id: str,
        lease_seconds: int = 300,
        *,
        kind: str | None = None,
        exclude_kinds: Sequence[str] = (),
    ) -> Job:
        """Atomically claim one pending job with a lease (P0-4).

        ``kind`` claims only that kind, oldest first; ``exclude_kinds`` leaves those
        kinds to their own pass. Without ``kind`` the order is unchanged.
        """
        now_value = datetime.now(UTC)
        now_iso = _iso(now_value)
        lease_iso = _iso(now_value + timedelta(seconds=lease_seconds))
        job_kind = records.c.payload["kind"].astext
        kind_filters = [job_kind == kind] if kind is not None else []
        if exclude_kinds:
            kind_filters.append(job_kind.not_in(list(exclude_kinds)))
        order = (records.c.payload["created_at"].astext, records.c.id) if kind is not None else (records.c.id,)
        pending_id = (
            select(records.c.id)
            .where(
                records.c.record_type == "job",
                records.c.status == "pending",
                or_(
                    records.c.retry_after.is_(None),
                    records.c.retry_after <= now_iso,
                ),
                *kind_filters,
            )
            .order_by(*order)
            .limit(1)
            .scalar_subquery()
        )
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    update(records)
                    .where(records.c.id == pending_id, records.c.status == "pending")
                    .values(status="running", lease_until=lease_iso)
                    .returning(records.c.id, records.c.payload)
                )
                .mappings()
                .one_or_none()
            )
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
        retry_in: float | None = None,
    ) -> Job:
        """Mark a claimed job as done or failed (atomically, owner-checked).

        ``retry_in`` (failed only) sends the job back to ``pending`` with the error kept, claimable again
        after that many seconds: for work that failed only because something it needs was busy.

        0.6.3: ``dead_letter`` ends the job where ``requeue_job`` and the drain alarm see it, with the
        error kept (for work that outlasted its retries only because something it needs was away)."""
        if status not in ("done", "failed", "dead_letter"):
            raise ValueError("job status must be 'done', 'failed' or 'dead_letter'")
        if retry_in is not None and status != "failed":
            raise ValueError("only a failed job can be retried later")
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(records.c.payload).where(
                        records.c.id == job_id,
                        records.c.record_type == "job",
                        records.c.status == "running",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFoundError("job not claimed")
            job = Job.model_validate(row["payload"])
            if worker_id is not None and job.worker_id != worker_id:
                raise ConflictError("job is owned by another worker")
            update_fields: dict[str, Any] = {
                "status": status,
                "last_error": error if status != "done" else None,
            }
            if status == "dead_letter":
                update_fields.update(dead_letter=True, lease_until=None)
            if status == "failed":
                wait = 5.0 if retry_in is None else retry_in
                update_fields["retry_after"] = datetime.now(UTC) + timedelta(seconds=wait)
            if retry_in is not None:
                update_fields.update(status="pending", worker_id=None, lease_until=None)
            updated = job.model_copy(update=update_fields)
            result = connection.execute(
                update(records)
                .where(
                    records.c.id == job_id,
                    records.c.record_type == "job",
                    records.c.status == "running",
                )
                .values(
                    status=updated.status,
                    payload=updated.model_dump(mode="json"),
                    retry_after=_iso(updated.retry_after),
                    lease_until=_iso(updated.lease_until),
                )
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
            rows = (
                connection.execute(
                    select(records.c.id, records.c.payload).where(
                        records.c.record_type == "job",
                        records.c.status == "running",
                        records.c.lease_until.is_not(None),
                        records.c.lease_until < now_iso,
                    )
                )
                .mappings()
                .all()
            )
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
                            payload=job.model_copy(update={"worker_id": None, "lease_until": None}).model_dump(
                                mode="json"
                            ),
                        )
                    )
                reaped += 1
        # HG follow-up (2026-08-22): stale outbox claims return to
        # ``pending`` so a crashed worker's projecting events are retried
        # instead of stranding. Events have no attempt budget — apply()
        # is idempotent, so unlimited retries converge.
        with self.engine.begin() as connection:
            stale = connection.execute(
                update(records)
                .where(
                    records.c.record_type == "outbox",
                    records.c.status == "projecting",
                    records.c.lease_until.is_not(None),
                    records.c.lease_until < now_iso,
                )
                .values(status="pending", lease_until=None)
            )
            reaped += stale.rowcount
        return reaped

    def list_dead_letter_jobs(self) -> list[Job]:
        """Inspect dead-lettered jobs (OPS-07)."""
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "job", records.c.status == "dead_letter")
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [Job.model_validate(value) for value in connection.execute(query).scalars()]

    def requeue_job(self, job_id: str) -> Job:
        """Recover a dead-lettered job: reset to pending with a fresh
        attempt budget (OPS-07). The job is retried on the next drain."""
        query = select(records.c.payload).where(
            records.c.id == job_id, records.c.record_type == "job", records.c.status == "dead_letter"
        )
        with self.engine.connect() as connection:
            value = connection.execute(query).scalar_one_or_none()
        if value is None:
            raise NotFoundError(f"dead-letter job {job_id!r} not found")
        job = Job.model_validate(value)
        requeued = job.model_copy(
            update={
                "status": "pending",
                "attempts": 0,
                "worker_id": None,
                "lease_until": None,
                "dead_letter": False,
            }
        )
        with self.engine.begin() as connection:
            connection.execute(
                update(records)
                .where(records.c.id == job_id, records.c.record_type == "job", records.c.status == "dead_letter")
                .values(status="pending", payload=requeued.model_dump(mode="json"))
            )
        return requeued

    def release_job(self, job_id: str, *, worker_id: str | None = None) -> Job:
        """Give a claimed job back to ``pending`` without using up an attempt (0.6.3)."""
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(records.c.payload).where(
                        records.c.id == job_id, records.c.record_type == "job", records.c.status == "running"
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFoundError("job not claimed")
            job = Job.model_validate(row["payload"])
            if worker_id is not None and job.worker_id != worker_id:
                raise ConflictError("job is owned by another worker")
            released = job.model_copy(
                update={
                    "status": "pending",
                    "attempts": max(0, job.attempts - 1),
                    "worker_id": None,
                    "lease_until": None,
                }
            )
            result = connection.execute(
                update(records)
                .where(records.c.id == job_id, records.c.record_type == "job", records.c.status == "running")
                .values(status="pending", payload=released.model_dump(mode="json"), lease_until=None)
            )
            if result.rowcount != 1:
                raise ConflictError("job release conflict")
        return released

    def list_jobs(self, *, status: str, kind: str | None = None) -> list[Job]:
        """Jobs in one state (and of one kind), oldest first (0.6.3)."""
        filters = [records.c.record_type == "job", records.c.status == status]
        if kind is not None:
            filters.append(records.c.payload["kind"].astext == kind)
        query = select(records.c.payload).where(*filters).order_by(records.c.payload["created_at"].astext, records.c.id)
        with self.engine.connect() as connection:
            return [Job.model_validate(value) for value in connection.execute(query).scalars()]

    def requeue_jobs(self, job_ids: Sequence[str], *, from_status: str = "failed") -> list[Job]:
        """Put ``failed``/``dead_letter`` jobs back to ``pending`` with a fresh budget, audited (0.6.3)."""
        if from_status not in REQUEUEABLE_JOB_STATUSES:
            raise ValueError("only failed or dead_letter jobs can be requeued")
        requeued: list[Job] = []
        with self.engine.begin() as connection:
            for job_id in dict.fromkeys(job_ids):
                value = connection.execute(
                    select(records.c.payload)
                    .where(records.c.id == job_id, records.c.record_type == "job", records.c.status == from_status)
                    .with_for_update()
                ).scalar_one_or_none()
                if value is None:
                    continue
                job = Job.model_validate(value)
                fresh = requeue_reset(job)
                result = connection.execute(
                    update(records)
                    .where(records.c.id == job_id, records.c.record_type == "job", records.c.status == from_status)
                    .values(status="pending", payload=fresh.model_dump(mode="json"), retry_after=None, lease_until=None)
                )
                if result.rowcount != 1:
                    continue
                self._insert_audit_in_transaction(
                    connection, job.scope, "job.requeue", job.id, requeue_audit(job, from_status)
                )
                requeued.append(fresh)
        return requeued

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

    def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 500,
        lease_seconds: int = 300,
    ) -> list[Event]:
        """Atomically claim up to ``limit`` pending events (HG follow-up).

        Fetch-and-mark in ONE transaction — each event row flips
        ``pending`` -> ``projecting`` and records the lease, so a second
        concurrent worker's claim transaction cannot see or flip the same
        rows. Mirrors :meth:`claim_job`'s atomic-update-then-return
        pattern. Stale ``projecting`` rows are returned to ``pending``
        by :meth:`reap_expired_jobs`.
        """
        now_value = datetime.now(UTC)
        lease_iso = _iso(now_value + timedelta(seconds=lease_seconds))
        claimed_ids = (
            select(records.c.id)
            .where(records.c.record_type == "outbox", records.c.status == "pending")
            .order_by(records.c.id)
            .limit(limit)
            .scalar_subquery()
        )
        with self.engine.begin() as connection:
            rows = connection.execute(
                update(records)
                .where(records.c.id.in_(claimed_ids), records.c.status == "pending")
                .values(status="projecting", lease_until=lease_iso)
                .returning(records.c.id, records.c.payload)
            ).mappings().all()
        return [Event.model_validate(row["payload"]) for row in rows]

    def release_outbox(self, event_ids: list[str]) -> None:
        """Return claimed events to ``pending`` immediately (idempotent).

        HG follow-up (2026-08-22): a failed batch must not hold its
        ``projecting`` claim until lease expiry — release lets the next
        pass retry at once. Only ``projecting`` rows are touched, so an
        ack racing a release still wins or loses cleanly.
        """
        if not event_ids:
            return
        with self.engine.begin() as connection:
            connection.execute(
                update(records)
                .where(
                    records.c.id.in_(event_ids),
                    records.c.record_type == "outbox",
                    records.c.status == "projecting",
                )
                .values(status="pending", lease_until=None)
            )

    def fail_outbox(self, event_ids: list[str], *, error: str, max_attempts: int) -> list[str]:
        """Record one failed projection attempt per event; return the ids now ``failed`` (0.6.4).

        An event under ``max_attempts`` goes back to ``pending`` (a later pass retries it, e.g. after a
        sidecar outage). At ``max_attempts`` it moves to ``failed``: the drain stops retrying it, the
        audit and ``severian outbox`` show it, and ``requeue_failed_outbox`` puts it back. It is never
        acked, so a memory whose vector could not be written can no longer disappear silently. The
        attempt count and the last error are kept in the event's payload.
        """
        if not event_ids:
            return []
        dead: list[str] = []
        with self.engine.begin() as connection:
            rows = connection.execute(
                select(records.c.id, records.c.payload).where(
                    records.c.id.in_(event_ids),
                    records.c.record_type == "outbox",
                    records.c.status.in_(("pending", "projecting")),
                )
            ).mappings().all()
            for row in rows:
                payload = dict(row["payload"])
                attempts = int(payload.get("projection_attempts", 0) or 0) + 1
                payload["projection_attempts"] = attempts
                payload["projection_error"] = error[:500]
                status = "failed" if attempts >= max_attempts else "pending"
                connection.execute(
                    update(records)
                    .where(records.c.id == row["id"])
                    .values(status=status, payload=payload, lease_until=None)
                )
                if status == "failed":
                    dead.append(str(row["id"]))
        return dead

    def failed_outbox(self) -> list[Event]:
        """Outbox events whose projection failed ``max_attempts`` times (0.6.4)."""
        with self.engine.connect() as connection:
            values = connection.execute(
                select(records.c.payload)
                .where(records.c.record_type == "outbox", records.c.status == "failed")
                .order_by(records.c.id)
            ).scalars()
            return [Event.model_validate(value) for value in values]

    def requeue_failed_outbox(self, event_ids: list[str] | None = None) -> int:
        """Put ``failed`` outbox events (all, or the given ids) back to ``pending`` with fresh attempts."""
        with self.engine.begin() as connection:
            query = select(records.c.id, records.c.payload).where(
                records.c.record_type == "outbox", records.c.status == "failed"
            )
            if event_ids is not None:
                query = query.where(records.c.id.in_(event_ids))
            rows = connection.execute(query).mappings().all()
            for row in rows:
                payload = {**dict(row["payload"]), "projection_attempts": 0}
                connection.execute(
                    update(records).where(records.c.id == row["id"]).values(status="pending", payload=payload)
                )
        return len(rows)

    def ack_outbox(self, event_id: str) -> None:
        with self.engine.begin() as connection:
            result = connection.execute(
                update(records)
                .where(records.c.id == event_id, records.c.record_type == "outbox",
                       records.c.status.in_(("pending", "projecting")))
                .values(status="acked")
            )
            if result.rowcount != 1:
                raise NotFoundError("outbox event not found or already acked")

    def ack_outbox_many(self, event_ids: list[str]) -> None:
        """Ack a batch of outbox events in ONE transaction (zero-debt 3.3).

        Missing/already-acked ids are ignored (idempotent) — a crash
        between apply and ack leaves events pending; re-acking a batch
        that partially landed must not raise.

        Chunked: PostgreSQL caps bind parameters at 65535 per statement
        (psycopg: "number of parameters must be between 0 and 65535").
        A 100k-event warmup pass acked the whole scope in ONE IN clause
        and blew past the cap (found in scale B4, 2026-08-09). Slices of
        _ACK_CHUNK are safely under both the PG protocol limit and
        SQLite's SQLITE_MAX_VARIABLE_NUMBER (32766 since 3.32).
        """
        if not event_ids:
            return
        for start in range(0, len(event_ids), _ACK_CHUNK):
            chunk = event_ids[start : start + _ACK_CHUNK]
            with self.engine.begin() as connection:
                connection.execute(
                    update(records)
                    .where(
                        records.c.id.in_(chunk),
                        records.c.record_type == "outbox",
                        records.c.status.in_(("pending", "projecting")),
                    )
                    .values(status="acked")
                )

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
                existing_identities = {key: row["id"] for row in existing_rows if (key := identity(row)) is not None}
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

    def _require_collection(self, scope: Scope) -> None:
        """DATA-05: a scope must not reference a collection owned by another tenant.

        Ownership semantics: if a collection with the scope's collection_id
        EXISTS, it must be owned by the scope's tenant. A missing collection
        is permitted (the product supports unprovisioned, collection-less
        operation; the service layer auto-provisions on ingest). A scope
        for tenant t1 referencing a collection owned by t2 is rejected —
        that is the ownership boundary this check enforces.
        """
        self._require_scope(scope)
        with self.engine.connect() as connection:
            row = connection.execute(
                select(records.c.tenant_id).where(
                    records.c.record_type == "collection",
                    records.c.id == scope.collection_id,
                )
            ).scalar_one_or_none()
        if row is not None and row != scope.tenant_id:
            raise NotFoundError(
                f"collection {scope.collection_id!r} is owned by tenant {row!r}, "
                f"not {scope.tenant_id!r}"
            )


__all__ = [
    "PostgreSQLBackend",
    "ConflictError",
    "NotFoundError",
]
