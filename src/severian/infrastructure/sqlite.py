from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, TypeVar

from sqlalchemy import JSON, Column, Index, MetaData, String, Table, create_engine, func, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from severian.domain.models import AuditEvent, Collection, Entity, Event, Job, Memory, Observation, Relation, Scope

T = TypeVar("T")


class ConflictError(RuntimeError):
    pass


class NotFoundError(LookupError):
    pass


metadata = MetaData()
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
    Column("payload", JSON, nullable=False),
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
    sqlite_where=records.c.identity_key.is_not(None),
)


class SQLiteRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.engine: Engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"timeout": 30},
        )

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")
            connection.exec_driver_sql("PRAGMA busy_timeout=30000")

    def close(self) -> None:
        self.engine.dispose()

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
        return {
            "id": model.id,
            "record_type": kind,
            **scope_values,
            "content_hash": content_hash,
            "identity_key": identity_key,
            "status": status,
            "payload": model.model_dump(mode="json"),
        }

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
                connection.execute(
                    records.insert().values(**self._record_values("outbox", event, status="pending"))
                )
                connection.execute(records.insert().values(**self._record_values("audit", audit)))
        return model

    @staticmethod
    def _scope_predicates(scope: Scope) -> list[Any]:
        return [getattr(records.c, key) == value for key, value in scope.model_dump().items()]

    def _get(self, kind: str, record_id: str, model: type[T], *, scope: Scope | None = None) -> T:
        predicates: list[Any] = [records.c.id == record_id, records.c.record_type == kind]
        if scope is not None:
            predicates.extend(self._scope_predicates(scope))
        with self.engine.connect() as connection:
            row = connection.execute(select(records.c.payload).where(*predicates)).scalar_one_or_none()
        if row is None:
            raise NotFoundError(f"{kind} not found")
        return model.model_validate(row)  # type: ignore[attr-defined,no-any-return]

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

    def add_observation(self, observation: Observation) -> Observation:
        digest = hashlib.sha256(observation.content.encode()).hexdigest()
        candidate = observation.model_copy(update={"content_hash": digest})
        identity_key = self._identity(digest)
        query = select(records.c.payload).where(
            records.c.record_type == "observation",
            *self._scope_predicates(candidate.scope),
            records.c.identity_key == identity_key,
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Observation.model_validate(existing)
        try:
            return self._insert(
                "observation",
                candidate,
                content_hash=digest,
                identity_key=identity_key,
                emit_events=True,
            )
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(query).scalar_one()
            return Observation.model_validate(existing)

    def get_observation(self, scope: Scope, observation_id: str) -> Observation:
        return self._get("observation", observation_id, Observation, scope=scope)

    def list_observations(self, scope: Scope) -> list[Observation]:
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
                    func.json_extract(records.c.payload, "$.superseded_by_id").is_(None),
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
        try:
            return self._insert("memory", memory, identity_key=identity_key, emit_events=True)
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(query).scalar_one_or_none()
            if existing is None:
                raise ConflictError("memory identity conflict") from None
            return Memory.model_validate(existing)

    def get_memory(self, scope: Scope, memory_id: str) -> Memory:
        return self._get("memory", memory_id, Memory, scope=scope)

    def list_memories(self, scope: Scope) -> list[Memory]:
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "memory", *self._scope_predicates(scope))
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [Memory.model_validate(value) for value in connection.execute(query).scalars()]

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
                    func.json_extract(records.c.payload, "$.superseded_by_id").is_(None),
                )
                .values(payload=changed.model_dump(mode="json"))
            )
            if result.rowcount != 1:
                raise ConflictError("memory revision conflict")
            connection.execute(records.insert().values(**self._record_values("memory", revision)))
            self._insert_events_in_transaction(connection, "memory", revision, operation="revise")
        return revision

    @classmethod
    def _insert_events_in_transaction(
        cls,
        connection: Connection,
        kind: str,
        model: Any,
        *,
        operation: str = "create",
    ) -> None:
        if (events := cls._event_models(kind, model, operation=operation)) is None:
            return
        event, audit = events
        connection.execute(records.insert().values(**cls._record_values("outbox", event, status="pending")))
        connection.execute(records.insert().values(**cls._record_values("audit", audit)))

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity:
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
        try:
            return self._insert("entity", entity, identity_key=identity_key, emit_events=True)
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(query).scalar_one()
            return Entity.model_validate(existing)

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
        try:
            return self._insert("relation", relation, identity_key=identity_key, emit_events=True)
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(query).scalar_one_or_none()
            if existing is None:
                raise ConflictError("relation identity conflict") from None
            return Relation.model_validate(existing)

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job:
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
        try:
            return self._insert("job", job, identity_key=identity_key, status="pending")
        except IntegrityError:
            with self.engine.connect() as connection:
                existing = connection.execute(query).scalar_one_or_none()
            if existing is None:
                raise ConflictError("job identity conflict") from None
            return Job.model_validate(existing)

    def claim_job(self, worker_id: str) -> Job:
        pending_id = (
            select(records.c.id)
            .where(records.c.record_type == "job", records.c.status == "pending")
            .order_by(records.c.id)
            .limit(1)
            .scalar_subquery()
        )
        with self.engine.begin() as connection:
            row = connection.execute(
                update(records)
                .where(records.c.id == pending_id, records.c.status == "pending")
                .values(status="running")
                .returning(records.c.id, records.c.payload)
            ).mappings().one_or_none()
            if row is None:
                raise NotFoundError("no pending job")
            job = Job.model_validate(row["payload"]).model_copy(update={"status": "running", "worker_id": worker_id})
            connection.execute(
                update(records).where(records.c.id == row["id"]).values(payload=job.model_dump(mode="json"))
            )
        return job

    def append_outbox(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Event:
        return self._insert("outbox", Event(scope=scope, kind=kind, payload=payload), status="pending")

    def pending_outbox(self) -> list[Event]:
        with self.engine.connect() as connection:
            values = connection.execute(
                select(records.c.payload)
                .where(records.c.record_type == "outbox", records.c.status == "pending")
                .order_by(records.c.id)
            ).scalars()
            return [Event.model_validate(value) for value in values]

    def append_audit(self, scope: Scope, action: str, target_id: str, metadata_: dict[str, Any]) -> AuditEvent:
        return self._insert("audit", AuditEvent(scope=scope, action=action, target_id=target_id, metadata=metadata_))

    def list_audit(self, scope: Scope) -> list[AuditEvent]:
        query = (
            select(records.c.payload)
            .where(records.c.record_type == "audit", *self._scope_predicates(scope))
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [AuditEvent.model_validate(value) for value in connection.execute(query).scalars()]

    def backup(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(prefix=f".{destination.name}.", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            source = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

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
