from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, cast

from sqlalchemy import JSON, Column, MetaData, String, Table, UniqueConstraint, create_engine, select, update
from sqlalchemy.engine import Engine

from severian.domain.models import AuditEvent, Collection, Entity, Event, Job, Memory, Observation, Relation, Scope

T = TypeVar("T")


class ConflictError(RuntimeError):
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
    Column("status", String, index=True),
    Column("payload", JSON, nullable=False),
    UniqueConstraint(
        "record_type",
        "tenant_id",
        "profile_id",
        "collection_id",
        "agent_id",
        "session_id",
        "content_hash",
        name="uq_observation_content",
    ),
)


class SQLiteRepository:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.engine: Engine = create_engine(f"sqlite:///{self.path}")

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata.create_all(self.engine)
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.engine.dispose()

    @staticmethod
    def _scope_values(scope: Scope | None) -> dict[str, str | None]:
        if scope is None:
            return {key: None for key in ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")}
        return scope.model_dump()

    def _insert(self, kind: str, model: Any, *, content_hash: str | None = None, status: str | None = None) -> Any:
        scope = getattr(model, "scope", None)
        values = {
            "id": model.id,
            "record_type": kind,
            **self._scope_values(scope),
            "content_hash": content_hash,
            "status": status,
            "payload": model.model_dump(mode="json"),
        }
        with self.engine.begin() as connection:
            connection.execute(records.insert().values(**values))
        return model

    def _get(self, kind: str, record_id: str, model: type[T]) -> T:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(records.c.payload).where(records.c.id == record_id, records.c.record_type == kind)
            ).scalar_one()
        return model.model_validate(row)  # type: ignore[attr-defined,no-any-return]

    def create_collection(self, collection: Collection) -> Collection:
        return cast(Collection, self._insert("collection", collection))

    def add_observation(self, observation: Observation) -> Observation:
        digest = hashlib.sha256(observation.content.encode()).hexdigest()
        candidate = observation.model_copy(update={"content_hash": digest})
        query = select(records.c.payload).where(
            records.c.record_type == "observation",
            *[getattr(records.c, key) == value for key, value in candidate.scope.model_dump().items()],
            records.c.content_hash == digest,
        )
        with self.engine.connect() as connection:
            existing = connection.execute(query).scalar_one_or_none()
        if existing is not None:
            return Observation.model_validate(existing)
        return cast(Observation, self._insert("observation", candidate, content_hash=digest))

    def get_observation(self, observation_id: str) -> Observation:
        return self._get("observation", observation_id, Observation)

    def list_observations(self, scope: Scope) -> list[Observation]:
        query = (
            select(records.c.payload)
            .where(
                records.c.record_type == "observation",
                *[getattr(records.c, key) == value for key, value in scope.model_dump().items()],
            )
            .order_by(records.c.id)
        )
        with self.engine.connect() as connection:
            return [Observation.model_validate(value) for value in connection.execute(query).scalars()]

    def update_observation_content(self, observation_id: str, content: str) -> None:
        del observation_id, content
        raise ConflictError("observations are immutable; create a revision")

    def revise_observation(self, observation_id: str, content: str) -> Observation:
        original = self.get_observation(observation_id)
        revision = self.add_observation(
            Observation(scope=original.scope, content=content, source=original.source, revision_of_id=original.id)
        )
        changed = original.model_copy(update={"superseded_by_id": revision.id})
        with self.engine.begin() as connection:
            connection.execute(
                update(records).where(records.c.id == original.id).values(payload=changed.model_dump(mode="json"))
            )
        return revision

    def add_memory(self, memory: Memory) -> Memory:
        for source_id in memory.source_observation_ids:
            self.get_observation(source_id)
        return cast(Memory, self._insert("memory", memory))

    def get_memory(self, memory_id: str) -> Memory:
        return self._get("memory", memory_id, Memory)

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity:
        return cast(Entity, self._insert("entity", Entity(scope=scope, kind=kind, name=name)))

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
        relation = Relation(
            scope=scope,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        return cast(Relation, self._insert("relation", relation))

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job:
        return cast(Job, self._insert("job", Job(scope=scope, kind=kind, payload=payload), status="pending"))

    def claim_job(self, worker_id: str) -> Job:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    select(records)
                    .where(records.c.record_type == "job", records.c.status == "pending")
                    .order_by(records.c.id)
                    .limit(1)
                )
                .mappings()
                .one()
            )
            job = Job.model_validate(row["payload"]).model_copy(update={"status": "running", "worker_id": worker_id})
            connection.execute(
                update(records)
                .where(records.c.id == job.id, records.c.status == "pending")
                .values(status="running", payload=job.model_dump(mode="json"))
            )
        return job

    def append_outbox(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Event:
        return cast(Event, self._insert("outbox", Event(scope=scope, kind=kind, payload=payload), status="pending"))

    def pending_outbox(self) -> list[Event]:
        with self.engine.connect() as connection:
            values = connection.execute(
                select(records.c.payload).where(records.c.record_type == "outbox", records.c.status == "pending")
            ).scalars()
            return [Event.model_validate(value) for value in values]

    def append_audit(self, scope: Scope, action: str, target_id: str, metadata_: dict[str, Any]) -> AuditEvent:
        return cast(
            AuditEvent,
            self._insert("audit", AuditEvent(scope=scope, action=action, target_id=target_id, metadata=metadata_)),
        )

    def list_audit(self, scope: Scope) -> list[AuditEvent]:
        query = select(records.c.payload).where(
            records.c.record_type == "audit",
            records.c.tenant_id == scope.tenant_id,
            records.c.profile_id == scope.profile_id,
            records.c.collection_id == scope.collection_id,
            records.c.agent_id == scope.agent_id,
            records.c.session_id == scope.session_id,
        )
        with self.engine.connect() as connection:
            return [AuditEvent.model_validate(value) for value in connection.execute(query).scalars()]

    def backup(self, destination: Path) -> None:
        self.engine.dispose()
        source = sqlite3.connect(self.path)
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()

    def export_json(self) -> str:
        with self.engine.connect() as connection:
            data = [dict(row) for row in connection.execute(select(records)).mappings()]
        return json.dumps({"format": "severian-v1", "records": data}, sort_keys=True)

    def import_json(self, payload: str) -> None:
        document = json.loads(payload)
        if document.get("format") != "severian-v1":
            raise ValueError("unsupported export format")
        with self.engine.begin() as connection:
            for record in document["records"]:
                connection.execute(records.insert().prefix_with("OR IGNORE").values(**record))
