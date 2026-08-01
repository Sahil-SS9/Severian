"""Live runtime conformance for the backend-neutral contract.

The unit-level contract tests prove the *shape* of the protocol. These
tests prove the *runtime behaviour* is identical across backends: the
reference ``SQLiteRepository`` and the live ``PostgreSQLBackend`` must
produce the same observable results for the same operations.

PostgreSQL tests require a live server. They are skipped unless
``SEVERIAN_PG_DSN`` is set (see ``docs/postgresql-backend.md`` for the
one-liner to start one). The suite is still exercised fully in CI /
on the dev rig where the container is running.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from severian.domain.models import (
    AuditEvent,
    Collection,
    Memory,
    Observation,
    Scope,
)
from severian.infrastructure.postgresql import PostgreSQLBackend
from severian.infrastructure.sqlite import ConflictError, NotFoundError, SQLiteRepository

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")


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


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path):
    """Parametrized backend fixture: runs every test on both backends.

    The ``postgres`` param requires a live server (skipped otherwise),
    so the whole conformance suite executes against both when the
    container is up.
    """
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("SEVERIAN_PG_DSN not set; skipping live PostgreSQL conformance")
        repo = PostgreSQLBackend(dsn=PG_DSN)
        repo.reset()  # isolated shared server between tests
    else:
        repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    yield repo
    repo.close()


def _unique_scope(**overrides: str) -> Scope:
    """Scope unique per test run so parametrized runs don't collide in shared Postgres."""
    base = {
        "tenant_id": f"t-{uuid.uuid4().hex[:8]}",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }
    base.update(overrides)
    return Scope(**base)


# -- canonical writes ------------------------------------------------------


def test_observation_roundtrip_and_scope_isolation(backend: Any) -> None:
    scope = _unique_scope()
    obs = Observation(scope=scope, content="Ada founded a company")
    stored = backend.add_observation(obs)

    assert backend.get_observation(scope, stored.id).id == stored.id

    other = scope.model_copy(update={"profile_id": "p2"})
    with pytest.raises(NotFoundError):
        backend.get_observation(other, stored.id)

    listed = backend.list_observations(scope)
    assert [item.id for item in listed] == [stored.id]


def test_observation_idempotency_by_content(backend: Any) -> None:
    scope = _unique_scope()
    first = backend.add_observation(Observation(scope=scope, content="same words"))
    second = backend.add_observation(Observation(scope=scope, content="same words"))
    assert first.id == second.id
    assert len(backend.list_observations(scope)) == 1


def test_observation_revision_is_immutable_chain(backend: Any) -> None:
    scope = _unique_scope()
    original = backend.add_observation(Observation(scope=scope, content="v1"))
    revision = backend.revise_observation(scope, original.id, "v2")

    assert revision.revision_of_id == original.id
    superseded = backend.get_observation(scope, original.id)
    assert superseded.superseded_by_id == revision.id

    # Both rows remain in the scope's list; revisions are append-only.
    assert len(backend.list_observations(scope)) == 2

    with pytest.raises(ConflictError):
        backend.revise_observation(scope, original.id, "v3")  # already superseded


def test_observation_immutability(backend: Any) -> None:
    scope = _unique_scope()
    obs = backend.add_observation(Observation(scope=scope, content="frozen"))
    with pytest.raises(ConflictError):
        backend.update_observation_content(obs.id, "mutated")


def test_memory_roundtrip_and_source_scope_check(backend: Any) -> None:
    scope = _unique_scope()
    obs = backend.add_observation(Observation(scope=scope, content="source note"))
    memory = Memory(
        scope=scope,
        kind="fact",
        content="Derived memory",
        source_observation_ids=[obs.id],
        model_id="local/model",
        prompt_version="prompt-v1",
        confidence=0.9,
    )
    stored = backend.add_memory(memory)
    assert backend.get_memory(scope, stored.id).id == stored.id

    # A memory whose source observation lives in another scope is rejected.
    other_obs = backend.add_observation(
        Observation(scope=scope.model_copy(update={"profile_id": "p2"}), content="elsewhere")
    )
    with pytest.raises(ConflictError):
        backend.add_memory(
            Memory(
                scope=scope,
                kind="fact",
                content="bad source",
                source_observation_ids=[other_obs.id],
                model_id="local/model",
                prompt_version="prompt-v1",
                confidence=0.5,
            )
        )


def test_memory_idempotency_by_derivation_identity(backend: Any) -> None:
    scope = _unique_scope()
    obs = backend.add_observation(Observation(scope=scope, content="stable"))
    base = dict(
        scope=scope,
        kind="fact",
        content="same derivation",
        source_observation_ids=[obs.id],
        model_id="local/model",
        prompt_version="prompt-v1",
        confidence=0.8,
    )
    first = backend.add_memory(Memory(**base))
    second = backend.add_memory(Memory(**base))
    assert first.id == second.id
    assert len(backend.list_memories(scope)) == 1


def test_memory_revision(backend: Any) -> None:
    scope = _unique_scope()
    obs = backend.add_observation(Observation(scope=scope, content="source"))
    memory = backend.add_memory(
        Memory(
            scope=scope,
            kind="fact",
            content="old",
            source_observation_ids=[obs.id],
            model_id="local/model",
            prompt_version="prompt-v1",
            confidence=0.9,
        )
    )
    revision = backend.revise_memory(
        scope,
        memory.id,
        content="new",
        model_id="local/model",
        prompt_version="prompt-v2",
    )
    assert revision.revision_of_id == memory.id
    assert backend.get_memory(scope, memory.id).superseded_by_id == revision.id


def test_entity_and_relation_with_scope_checks(backend: Any) -> None:
    scope = _unique_scope()
    alice = backend.add_entity(scope, "person", "Alice")
    bob = backend.add_entity(scope, "person", "Bob")
    relation = backend.add_relation(
        scope,
        alice.id,
        "knows",
        bob.id,
        valid_from=datetime(2026, 1, 1),
        valid_to=datetime(2026, 12, 31),
    )
    assert relation.predicate == "knows"
    assert relation.valid_from is not None

    # Cross-scope endpoint must fail.
    other = scope.model_copy(update={"session_id": "other"})
    stranger = backend.add_entity(other, "person", "Stranger")
    with pytest.raises(ConflictError):
        backend.add_relation(scope, alice.id, "knows", stranger.id)


def test_entity_idempotency(backend: Any) -> None:
    scope = _unique_scope()
    first = backend.add_entity(scope, "person", "Alice")
    second = backend.add_entity(scope, "person", "alice")  # casefold identity
    assert first.id == second.id


# -- jobs ------------------------------------------------------------------


def test_job_enqueue_claim_complete_lifecycle(backend: Any) -> None:
    scope = _unique_scope()
    job = backend.enqueue_job(scope, "derive", {"observation_id": "abc"})
    assert job.status == "pending"

    claimed = backend.claim_job("worker-1")
    assert claimed.id == job.id
    assert claimed.status == "running"
    assert claimed.worker_id == "worker-1"

    done = backend.complete_job(job.id, "done")
    assert done.status == "done"

    with pytest.raises(NotFoundError):
        backend.claim_job("worker-2")  # queue empty


def test_job_completion_requires_claimed_state(backend: Any) -> None:
    scope = _unique_scope()
    job = backend.enqueue_job(scope, "derive", {"observation_id": "abc"})
    with pytest.raises(NotFoundError):
        backend.complete_job(job.id, "done")  # not claimed -> not found


def test_job_idempotency(backend: Any) -> None:
    scope = _unique_scope()
    first = backend.enqueue_job(scope, "derive", {"observation_id": "abc"})
    second = backend.enqueue_job(scope, "derive", {"observation_id": "abc"})
    assert first.id == second.id


# -- outbox and audit ------------------------------------------------------


def test_outbox_and_audit_append(backend: Any) -> None:
    scope = _unique_scope()
    event = backend.append_outbox(scope, "memory.created", {"id": "m1"})
    assert event.kind == "memory.created"
    pending = backend.pending_outbox()
    assert any(item.id == event.id for item in pending)

    audit = backend.append_audit(scope, "memory.create", "m1", {"note": "x"})
    assert isinstance(audit, AuditEvent)
    if hasattr(backend, "list_audit"):
        listed = backend.list_audit(scope)
        assert any(item.id == audit.id for item in listed)


# -- export / import -------------------------------------------------------


def test_export_import_roundtrip(backend: Any) -> None:
    scope = _unique_scope()
    backend.add_observation(Observation(scope=scope, content="export me"))
    payload = backend.export_json()

    # Import into a fresh instance of the same backend class.
    if isinstance(backend, PostgreSQLBackend):
        other = PostgreSQLBackend(dsn=PG_DSN)
    else:
        other = SQLiteRepository(backend.path.parent / "other.db")
    other.initialize()
    other.import_json(payload)
    assert len(other.list_observations(scope)) == 1
    other.close()


def test_export_checksum_rejects_tampering(backend: Any) -> None:
    scope = _unique_scope()
    backend.add_observation(Observation(scope=scope, content="tamper me"))
    payload = backend.export_json()

    document = json.loads(payload)
    document["records"][0]["content_hash"] = "deadbeef"
    with pytest.raises(ValueError):
        backend.import_json(json.dumps(document))


def test_import_rejects_rechecksummed_scope_metadata_forgery(backend: Any) -> None:
    scope = _unique_scope()
    backend.add_observation(Observation(scope=scope, content="private"))
    document = json.loads(backend.export_json())
    observation_record = next(item for item in document["records"] if item["record_type"] == "observation")
    observation_record["tenant_id"] = "attacker"
    document["checksum"] = backend.export_checksum(document["records"])

    with pytest.raises(ValueError, match="payload metadata"):
        backend.import_json(json.dumps(document))


def test_backup_writes_restorable_snapshot(backend: Any) -> None:
    scope = _unique_scope()
    backend.add_observation(Observation(scope=scope, content="backup me"))
    destination = Path(f"/tmp/severian-conformance-{id(backend)}.json")
    backend.backup(destination)
    assert destination.exists()
    assert destination.stat().st_size > 0
    if isinstance(backend, PostgreSQLBackend):
        # Postgres backup() is a portable JSON export document.
        assert "severian-v1" in destination.read_text(encoding="utf-8")
    destination.unlink(missing_ok=True)


# -- scope guard -----------------------------------------------------------


@pytest.mark.parametrize(
    "dimension",
    ["tenant_id", "profile_id", "collection_id", "agent_id", "session_id"],
)
def test_blank_scope_dimension_rejected(backend: Any, dimension: str) -> None:
    # model_copy bypasses Scope construction validation, forcing the
    # backend's own fail-closed guard to fire (contract defence-in-depth).
    scope = _scope().model_copy(update={dimension: "  "})
    with pytest.raises(ValueError):
        backend.get_observation(scope, "whatever")
