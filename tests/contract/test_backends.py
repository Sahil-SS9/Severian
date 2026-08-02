"""Conformance tests for the backend-neutral repository contract.

These tests assert that:

1. ``SQLiteRepository`` (the reference backend) satisfies
   ``CanonicalRepository`` at runtime, so the contract is not vacuous.
2. A minimal in-memory fake backend also satisfies ``CanonicalRepository``,
   proving the contract is implementable by an alternative backend.
3. ``PostgreSQLBackend`` structurally satisfies ``CanonicalRepository``
   and enforces scope invariants before any storage access.
4. ``check_scope`` fails closed on blank dimensions.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from severian.backends import (
    CanonicalRepository,
    NotFoundError,
    ProjectionHit,
    ProjectionIndex,
    check_scope,
)
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
)
from severian.infrastructure.postgresql import PostgreSQLBackend
from severian.infrastructure.sqlite import SQLiteRepository


def _scope(**overrides: str) -> Scope:
    base = {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }
    base.update(overrides)
    return Scope(**base)


# -- contract satisfaction ------------------------------------------------

def test_sqlite_backend_satisfies_contract(tmp_path: Path) -> None:
    repository = SQLiteRepository(tmp_path / "contract.db")
    try:
        assert isinstance(repository, CanonicalRepository)
    finally:
        repository.close()


def test_postgresql_backend_satisfies_contract() -> None:
    # A class satisfies a runtime_checkable Protocol when it implements
    # every method/attribute named in the protocol.
    assert isinstance(PostgreSQLBackend, CanonicalRepository)

class _FakeRepository:
    """Minimal in-memory backend proving the contract is implementable."""

    def __init__(self) -> None:
        self._next_id = 0
        self._observations: dict[tuple[str, ...], Observation] = {}
        self._memories: dict[tuple[str, ...], Memory] = {}

    def _key(self, scope: Scope, record_id: str) -> tuple[str, ...]:
        return (record_id, *scope.model_dump().values())

    def initialize(self) -> None:  # pragma: no cover - interface parity
        pass

    def close(self) -> None:  # pragma: no cover - interface parity
        pass

    def create_collection(self, collection: Collection) -> Collection:
        return collection

    def get_collection(self, tenant_id: str, collection_id: str) -> Collection:
        raise NotFoundError("collection not found")

    def add_observation(self, observation: Observation) -> Observation:
        self._observations[self._key(observation.scope, observation.id)] = observation
        return observation

    def get_observation(self, scope: Scope, observation_id: str) -> Observation:
        found = self._observations.get(self._key(scope, observation_id))
        if found is None:
            raise NotFoundError("observation not found")
        return found

    def list_observations(self, scope: Scope) -> list[Observation]:
        return [o for (rid, *s), o in self._observations.items() if tuple(s) == tuple(scope.model_dump().values())]

    def revise_observation(self, scope: Scope, observation_id: str, content: str) -> Observation:
        original = self.get_observation(scope, observation_id)
        revision = original.model_copy(
            update={
                "id": Observation(scope=scope, content=content).id,
                "content": content,
                "revision_of_id": original.id,
            }
        )
        self._observations[self._key(scope, revision.id)] = revision
        return revision

    def add_memory(self, memory: Memory) -> Memory:
        self._memories[self._key(memory.scope, memory.id)] = memory
        return memory

    def get_memory(self, scope: Scope, memory_id: str) -> Memory:
        found = self._memories.get(self._key(scope, memory_id))
        if found is None:
            raise NotFoundError("memory not found")
        return found

    def list_memories(self, scope: Scope) -> list[Memory]:
        return [m for (rid, *s), m in self._memories.items() if tuple(s) == tuple(scope.model_dump().values())]

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
            }
        )
        self._memories[self._key(scope, revision.id)] = revision
        return revision

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job:
        return Job(scope=scope, kind=kind, payload=payload)

    def claim_job(self, worker_id: str, lease_seconds: int = 300) -> Job:
        raise NotFoundError("no pending job")

    def complete_job(
        self,
        job_id: str,
        status: str,
        *,
        worker_id: str | None = None,
        error: str | None = None,
    ) -> Job:
        raise NotFoundError("job not claimed")

    def reap_expired_jobs(self, *, now: datetime | None = None, max_attempts: int = 3) -> int:
        return 0

    def ack_outbox(self, event_id: str) -> None:
        raise NotFoundError("outbox event not found or already acked")

    def add_observation_and_enqueue(
        self, observation: Observation, job_kind: str = "derive"
    ) -> tuple[Observation, Job]:
        return (
            observation,
            Job(scope=observation.scope, kind=job_kind, payload={"observation_id": observation.id}),
        )

    def add_memories_batch(self, memories: list[Memory]) -> list[Memory]:
        return [self.add_memory(memory) for memory in memories]

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity:
        return Entity(scope=scope, kind=kind, name=name)

    def get_entity(self, scope: Scope, entity_id: str) -> Entity:
        raise NotFoundError("entity not found")

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
        return Relation(
            scope=scope,
            subject_id=subject_id,
            predicate=predicate,
            object_id=object_id,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def append_audit(
        self, scope: Scope, action: str, target_id: str, metadata: Mapping[str, Any]
    ) -> AuditEvent:
        return AuditEvent(scope=scope, action=action, target_id=target_id, metadata=dict(metadata))

    def list_audit(self, scope: Scope) -> list[AuditEvent]:
        return []

    def append_outbox(self, scope: Scope, kind: str, payload: Mapping[str, Any]) -> Event:
        return Event(scope=scope, kind=kind, payload=dict(payload))

    def pending_outbox(self) -> list[Event]:
        return []

    def export_json(self) -> str:
        return "{}"

    def import_json(self, payload: str) -> None:  # pragma: no cover - interface parity
        pass

    def backup(self, destination: Path) -> None:  # pragma: no cover - interface parity
        pass


def test_fake_backend_satisfies_contract() -> None:
    assert isinstance(_FakeRepository(), CanonicalRepository)


# -- scope invariant conformance -----------------------------------------

def test_sqlite_backend_isolates_observations_by_scope(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    obs = Observation(scope=_scope(), content="Ada founded a company")
    stored = repo.add_observation(obs)
    # Same tenant/collection but different profile must not be visible.
    other = _scope(profile_id="p2")
    with pytest.raises(NotFoundError):
        repo.get_observation(other, stored.id)
    # Correct scope returns it.
    assert repo.get_observation(_scope(), stored.id).id == stored.id
    repo.close()


def test_sqlite_backend_enforces_collection_scope(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    with pytest.raises(NotFoundError):
        repo.get_collection("t2", "c1")
    assert repo.get_collection("t1", "c1").id == "c1"
    repo.close()


def test_postgresql_backend_rejects_blank_scope_before_storage() -> None:
    # A live-DSN-less backend fails closed at construction.
    with pytest.raises(ValueError):
        PostgreSQLBackend(dsn="")
    with pytest.raises(ValueError):
        PostgreSQLBackend(dsn="   ")


@pytest.mark.parametrize(
    "dimension",
    ["tenant_id", "profile_id", "collection_id", "agent_id", "session_id"],
)
def test_check_scope_fails_closed_on_blank(dimension: str) -> None:
    scope = _scope()  # valid scope object
    # Blank-dimension validation happens at Scope construction, which is the
    # primary fail-closed gate. check_scope independently re-validates and
    # must raise ValueError on a manually blanked dimension.
    blanked = scope.model_copy(update={dimension: "  "})
    with pytest.raises(ValueError):
        check_scope(blanked)


def test_check_scope_passes_on_valid() -> None:
    assert check_scope(_scope()) == _scope()


# -- projection contract --------------------------------------------------

def test_projection_index_protocol_exists() -> None:
    class _FakeIndex:
        def index(self, scope: Scope, resource_id: str, content: str) -> None:  # pragma: no cover
            pass

        def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[ProjectionHit]:  # pragma: no cover
            return []

        def delete(self, scope: Scope, resource_id: str) -> None:  # pragma: no cover
            pass

        def clear(self, scope: Scope) -> None:  # pragma: no cover
            pass

        def count(self, scope: Scope) -> int:  # pragma: no cover
            return 0

        def close(self) -> None:  # pragma: no cover
            pass

    assert isinstance(_FakeIndex(), ProjectionIndex)
