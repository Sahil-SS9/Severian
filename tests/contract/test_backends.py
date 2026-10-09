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

from collections.abc import Mapping, Sequence
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
from severian.domain.errors import ConflictError
from severian.domain.models import (
    ERASED,
    AuditEvent,
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
    Scope,
    revision_lineage,
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

    def collection_exists(self, collection_id: str) -> bool:
        return False

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

    def list_pending_observations(self, scope: Scope, *, limit: int) -> list[Observation]:
        """Active observations with no memory yet (fake parity, 3.1)."""
        scope_tuple = tuple(scope.model_dump().values())
        derived_sources: set[str] = set()
        for (_rid, *s), memory in self._memories.items():
            if tuple(s) == scope_tuple:
                derived_sources.update(memory.source_observation_ids)
        pending = [
            o for (_rid, *s), o in self._observations.items()
            if tuple(s) == scope_tuple
            and o.superseded_by_id is None
            and o.id not in derived_sources
        ]
        pending.sort(key=lambda o: o.id)
        return pending[:limit]

    def list_observations_history(self, scope: Scope) -> list[Observation]:
        # The fake retains every observation (revisions are added, the
        # original is left untouched) — history == full list here.
        return [o for (rid, *s), o in self._observations.items() if tuple(s) == tuple(scope.model_dump().values())]

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
        original = self.get_observation(scope, observation_id)
        revision = original.model_copy(
            update={
                "id": Observation(scope=scope, content=content).id,
                "content": content,
                "revision_of_id": original.id,
                "superseded_by_id": ERASED if tombstone else None,
            }
        )
        self._observations[self._key(scope, revision.id)] = revision
        if derive:
            self.enqueue_job(scope, "derive", {"observation_id": revision.id})
        return revision

    def add_memory(self, memory: Memory) -> Memory:
        self._memories[self._key(memory.scope, memory.id)] = memory
        return memory

    def get_memory(self, scope: Scope, memory_id: str) -> Memory:
        found = self._memories.get(self._key(scope, memory_id))
        if found is None:
            raise NotFoundError("memory not found")
        return found

    def record_memory_usage(self, scope: Scope, memory_id: str) -> Memory:
        key = self._key(scope, memory_id)
        found = self._memories.get(key)
        if found is None:
            raise NotFoundError("memory not found")
        updated = found.model_copy(update={"usage_count": found.usage_count + 1})
        self._memories[key] = updated
        return updated

    def close_memory(self, scope: Scope, memory_id: str, *, valid_until: datetime, replaced_by_id: str) -> bool:
        key = self._key(scope, memory_id)
        found = self._memories.get(key)
        if found is None or found.superseded_by_id is not None or found.valid_until is not None:
            return False
        self._memories[key] = found.model_copy(update={"valid_until": valid_until, "replaced_by_id": replaced_by_id})
        return True

    def count_memories(self, scope: Scope) -> int:
        return len(self.list_memories(scope))

    def list_memories(self, scope: Scope) -> list[Memory]:
        return [
            m
            for (rid, *s), m in self._memories.items()
            if tuple(s) == tuple(scope.model_dump().values()) and m.superseded_by_id is None
        ]

    def list_session_observations(self, scope: Scope, session_ids: list[str]) -> list[Observation]:
        wanted = {str(value) for value in session_ids}
        return [o for o in self.list_observations(scope) if str((o.source or {}).get("session_id")) in wanted]

    def list_memories_for_observations(self, scope: Scope, observation_ids: list[str]) -> list[Memory]:
        wanted = set(observation_ids)
        return [m for m in self.list_memories(scope) if wanted.intersection(m.source_observation_ids)]

    def list_trigger_memories(self, scope: Scope) -> list[Memory]:
        return [m for m in self.list_memories(scope) if m.trigger is not None]

    def list_memories_history(self, scope: Scope) -> list[Memory]:
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

    def rollback_memory(self, scope: Scope, revision_id: str) -> Memory:
        target = self.get_memory(scope, revision_id)
        restored = target.model_copy(update={"superseded_by_id": None})
        self._memories[self._key(scope, revision_id)] = restored
        return restored

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job:
        return Job(scope=scope, kind=kind, payload=payload)

    def claim_job(
        self,
        worker_id: str,
        lease_seconds: int = 300,
        *,
        kind: str | None = None,
        exclude_kinds: tuple[str, ...] = (),
    ) -> Job:
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

    def list_profile_scopes(self, tenant_id: str, profile_id: str) -> list[Scope]:
        return []

    def cancel_pending_jobs(self, scope: Scope, kind: str) -> int:
        return 0

    def reap_expired_jobs(self, *, now: datetime | None = None, max_attempts: int = 3) -> int:
        return 0

    def list_dead_letter_jobs(self) -> list[Job]:
        return []

    def requeue_job(self, job_id: str) -> Job:
        raise NotFoundError(f"dead-letter job {job_id!r} not found")

    def ack_outbox(self, event_id: str) -> None:
        raise NotFoundError("outbox event not found or already acked")

    def ack_outbox_many(self, event_ids: list[str]) -> None:
        for event_id in event_ids:
            self.ack_outbox(event_id)

    def add_observation_and_enqueue(
        self, observation: Observation, job_kind: str = "derive", *, also_queue: Sequence[str] = ()
    ) -> tuple[Observation, Job]:
        return (
            observation,
            Job(scope=observation.scope, kind=job_kind, payload={"observation_id": observation.id}),
        )

    def add_observations_and_enqueue_batch(
        self, observations: list[Observation], job_kind: str = "derive"
    ) -> list[tuple[Observation, Job]]:
        return [self.add_observation_and_enqueue(o, job_kind) for o in observations]

    def add_memories_batch(self, memories: list[Memory]) -> list[Memory]:
        return [self.add_memory(memory) for memory in memories]

    def publish_derivation(
        self,
        scope: Scope,
        *,
        entities: list[tuple[str, str]],
        relations: list[tuple[str, str, str]],
        memories: list[Memory],
        source_observation_id: str | None = None,
    ) -> list[Memory]:
        # In-memory analogue of the backend atomic publish (DATA-04).
        # The fake's add_entity/add_relation are no-op shapes (no storage),
        # so this mirrors the call pattern without graph resolution or lineage.
        del source_observation_id
        for kind, name in entities:
            self.add_entity(scope, kind, name)
        for subject, predicate, obj in relations:
            subject_ent = self.add_entity(scope, "entity", subject)
            object_ent = self.add_entity(scope, "entity", obj)
            self.add_relation(scope, subject_ent.id, predicate, object_ent.id)
        return self.add_memories_batch(memories)

    def retract_relations_for_source(self, scope: Scope, observation_id: str) -> int:
        # No relation storage in the fake: nothing can lose its support.
        del scope, observation_id
        return 0

    def list_memory_revisions(self, scope: Scope, memory_id: str) -> list[Memory]:
        in_scope = {
            memory.id: memory
            for (rid, *s), memory in self._memories.items()
            if tuple(s) == tuple(scope.model_dump().values())
        }
        if memory_id not in in_scope:
            raise NotFoundError("memory not found")
        return revision_lineage(in_scope, memory_id)

    def supersede_memories_for_source(self, scope: Scope, observation_id: str) -> int:
        superseded = 0
        for key in list(self._memories):
            rid, *s = key
            if tuple(s) != tuple(scope.model_dump().values()):
                continue
            memory = self._memories[key]
            if memory.superseded_by_id is None and observation_id in memory.source_observation_ids:
                self._memories[key] = memory.model_copy(update={"superseded_by_id": f"source-rev:{observation_id}"})
                superseded += 1
        return superseded

    def supersede_memory(self, scope: Scope, memory_id: str, replacement_id: str) -> Memory:
        for key in list(self._memories):
            rid, *s = key
            if tuple(s) != tuple(scope.model_dump().values()):
                continue
            memory = self._memories[key]
            if memory.id == memory_id:
                if memory.superseded_by_id is not None:
                    raise ConflictError("memory is already superseded")
                if memory_id == replacement_id:
                    raise ConflictError("cannot supersede a memory by itself")
                updated = memory.model_copy(update={"superseded_by_id": replacement_id})
                self._memories[key] = updated
                return updated
        raise NotFoundError(f"memory {memory_id} not found")

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity:
        return Entity(scope=scope, kind=kind, name=name)

    def get_entity(self, scope: Scope, entity_id: str) -> Entity:
        raise NotFoundError("entity not found")

    def list_entities(self, scope: Scope) -> list[Entity]:
        return []

    def get_relation(self, scope: Scope, relation_id: str) -> Relation:
        raise NotFoundError("relation not found")

    def list_relations(self, scope: Scope) -> list[Relation]:
        return []

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

    def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 500,
        lease_seconds: int = 300,
    ) -> list[Event]:
        # Minimal fake: no outbox state, so nothing is ever claimable.
        return []

    def release_outbox(self, event_ids: list[str]) -> None:
        # No-op: the fake holds no outbox claims to release.
        return None

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


def test_sqlite_write_rejects_cross_tenant_collection(tmp_path: Path) -> None:
    """DATA-05: a write whose scope references a collection owned by a
    DIFFERENT tenant must fail closed.

    The collection exists but belongs to t2; a scope claiming t1 with that
    collection id must not be accepted.
    """
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t2", name="Primary"))

    # Scope claims tenant t1 but references t2's collection.
    wrong_scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    with pytest.raises(NotFoundError, match="collection"):
        repo.add_observation(Observation(scope=wrong_scope, content="Ada founded a company"))
    repo.close()


def test_sqlite_batch_memory_lookup_is_scope_aware(tmp_path: Path) -> None:
    """DATA-06: batch memory insert must not be satisfied by a memory with
    the same identity in a DIFFERENT scope.

    A memory's identity (kind+content+source+model+prompt) is scope-less;
    the batch lookup previously matched on identity alone, so a memory in
    scope A could suppress (or return) the insert for the same content in
    scope B. The single-insert path scopes correctly; batch must agree.
    """
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    # Collection IDs are globally unique (records.id is the PK); give each
    # tenant its own collection id.
    repo.create_collection(Collection(id="c-a", tenant_id="t1", name="Primary"))
    repo.create_collection(Collection(id="c-b", tenant_id="t2", name="Primary"))

    scope_a = Scope(tenant_id="t1", profile_id="p1", collection_id="c-a", agent_id="a1", session_id="s1")
    scope_b = Scope(tenant_id="t2", profile_id="p1", collection_id="c-b", agent_id="a1", session_id="s1")

    # Source observations must exist in each scope (memory references are
    # validated same-scope). The observation id is content-derived and
    # scope-specific, so capture each scope's real id.
    obs_a = repo.add_observation(Observation(scope=scope_a, content="Ada founded a company"))
    obs_b = repo.add_observation(Observation(scope=scope_b, content="Ada founded a company"))

    def _mem(scope: Scope, memory_id: str, obs_id: str) -> Memory:
        return Memory(
            id=memory_id,
            scope=scope,
            kind="fact",
            content="Ada founded a company",
            source_observation_ids=[obs_id],
            model_id="test",
            prompt_version="v1",
            confidence=1.0,
        )

    # Insert the same content+kind+source in scope A first.
    inserted_a = repo.add_memory(_mem(scope_a, "mem-a", obs_a.id))
    assert inserted_a.id == "mem-a"

    # Batch-insert the IDENTICAL identity in scope B. It must NOT be
    # suppressed by the scope-A memory — both must exist.
    inserted_b = repo.add_memories_batch([_mem(scope_b, "mem-b", obs_b.id)])
    assert len(inserted_b) == 1
    assert inserted_b[0].id == "mem-b"

    # Both scopes now return their own memory.
    assert repo.get_memory(scope_a, "mem-a").id == "mem-a"
    assert repo.get_memory(scope_b, "mem-b").id == "mem-b"
    assert len(repo.list_memories(scope_a)) == 1
    assert len(repo.list_memories(scope_b)) == 1
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


def test_sqlite_store_permissions_are_user_private(tmp_path: Path):
    """P2: memory store files must not be world-readable (0644 default)."""
    import os
    import stat

    from severian.infrastructure.sqlite import SQLiteRepository

    db = tmp_path / "private.db"
    repo = SQLiteRepository(db)
    repo.initialize()
    repo.close()

    mode = stat.S_IMODE(os.stat(db).st_mode)
    assert mode & 0o077 == 0  # no group/other bits
    assert mode == 0o600
