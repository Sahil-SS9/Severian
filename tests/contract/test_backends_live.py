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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event

from severian.application.dreaming import CurationService
from severian.application.worker import DerivationWorker
from severian.composition import build_bundle
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
        # Explicit acknowledgement: the shared local test server is the
        # only thing this fixture may wipe (P1-3).
        os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
        repo.reset(acknowledge=True)  # isolated shared server between tests
    else:
        repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    yield repo
    repo.close()


def _unique_scope(**overrides: str) -> Scope:
    """Scope unique per test run so parametrized runs don't collide in shared Postgres.

    The collection id is also unique per test (DATA-05: collection ids are
    globally unique; a shared 'c1' would collide with the module fixture's
    'c1' owned by tenant 't1' once collection ownership is enforced).
    """
    base = {
        "tenant_id": f"t-{uuid.uuid4().hex[:8]}",
        "profile_id": "p1",
        "collection_id": f"c-{uuid.uuid4().hex[:8]}",
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


def test_job_claim_can_skip_kinds(backend: Any) -> None:
    scope = _unique_scope()
    derive = backend.enqueue_job(scope, "derive", {"observation_id": "d1"})
    backend.enqueue_job(scope, "extract_facts", {"observation_id": "o1"})
    assert backend.claim_job("worker-1", exclude_kinds=("extract_facts",)).id == derive.id
    with pytest.raises(NotFoundError):
        backend.claim_job("worker-1", exclude_kinds=("extract_facts",))


def test_job_claim_by_kind_takes_only_that_kind_oldest_first(backend: Any) -> None:
    scope = _unique_scope()
    backend.enqueue_job(scope, "derive", {"observation_id": "d1"})
    queued = [backend.enqueue_job(scope, "extract_facts", {"observation_id": f"o{i}"}) for i in range(6)]
    # oldest first, whatever order the random ids sort in (the default claim orders by id)
    assert [backend.claim_job("worker-1", kind="extract_facts").id for _ in queued] == [j.id for j in queued]
    with pytest.raises(NotFoundError):
        backend.claim_job("worker-1", kind="extract_facts")
    assert backend.claim_job("worker-1").kind == "derive"


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


def test_batch_ingest_and_ack_many(backend: Any) -> None:
    """Live parity: batch ingest + batch ack behave like the singles."""
    scope = _unique_scope()
    observations = [
        Observation(scope=scope, content=f"batch live {i}", source={"uri": "live"})
        for i in range(15)
    ]
    pairs = backend.add_observations_and_enqueue_batch(observations)
    assert len(pairs) == 15
    assert len(backend.list_observations(scope)) == 15
    assert all(job.kind == "derive" for _, job in pairs)

    # Re-ingest idempotent: same ids returned, no dupes.
    again = backend.add_observations_and_enqueue_batch(observations)
    assert [o.id for o, _ in again] == [o.id for o, _ in pairs]
    assert len(backend.list_observations(scope)) == 15

    # Batch ack: all outbox events for the batch acked in one call.
    pending = backend.pending_outbox()
    created = [e for e in pending if e.kind == "observation.created"]
    assert len(created) == 15
    backend.ack_outbox_many([e.id for e in created])
    remaining = [e for e in backend.pending_outbox() if e.kind == "observation.created"]
    assert remaining == []


def test_batch_ingest_rolls_back_on_conflict(backend: Any) -> None:
    """Live parity: a mid-batch conflict must not half-commit (P0-4)."""
    scope = _unique_scope()
    dup = Observation(scope=scope, content="dup", id="dup-id-live")
    observations = [Observation(scope=scope, content=f"rollback {i}") for i in range(5)] + [dup, dup]
    with pytest.raises(Exception, match=r"IntegrityError|UNIQUE|conflict|duplicate key"):
        backend.add_observations_and_enqueue_batch(observations)
    assert len(backend.list_observations(scope)) == 0  # atomic rollback


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


# -- improvement 2: single-valued relations (both backends) ---------------


def _publish_fact(backend, scope: Scope, subject: str, predicate: str, obj: str) -> None:
    observation = backend.add_observation(
        Observation(scope=scope, content=f"{subject} {predicate} {obj}", source={"session_id": "s1"})
    )
    backend.publish_derivation(
        scope,
        entities=[("person", subject), ("organisation", obj)],
        relations=[(subject, predicate, obj)],
        memories=[],
        source_observation_id=observation.id,
    )


def _open_objects(backend, scope: Scope, predicate: str) -> set[str]:
    names = {entity.id: entity.name for entity in backend.list_entities(scope)}
    return {
        names[r.object_id] for r in backend.list_relations(scope)
        if r.predicate == predicate and r.valid_to is None and r.retracted_at is None
    }


def test_single_valued_relation_keeps_only_the_newest_value_open(backend) -> None:
    scope = _scope()
    _publish_fact(backend, scope, "Alice", "works_at", "Acme")
    _publish_fact(backend, scope, "Alice", "works_at", "Globex")
    assert _open_objects(backend, scope, "works_at") == {"Globex"}
    assert len([r for r in backend.list_relations(scope) if r.predicate == "works_at"]) == 2

    _publish_fact(backend, scope, "Alice", "works_at", "Acme")
    assert _open_objects(backend, scope, "works_at") == {"Acme"}


def test_multi_valued_relation_keeps_every_value_open(backend) -> None:
    scope = _scope()
    _publish_fact(backend, scope, "Alice", "works_with", "Bob")
    _publish_fact(backend, scope, "Alice", "works_with", "Carol")
    assert _open_objects(backend, scope, "works_with") == {"Bob", "Carol"}


# -- close_memory: a newer fact closes an older one without deleting it ------------


def _memory_for(backend, scope: Scope, text: str) -> Any:
    observation = backend.add_observation(Observation(scope=scope, content=f"obs for {text}"))
    return backend.add_memory(
        Memory(
            scope=scope,
            kind="user_fact",
            content=text,
            confidence=0.9,
            source_observation_ids=[observation.id],
            model_id="m",
            prompt_version="v",
        )
    )


_CLOSED_AT = datetime(2026, 1, 28, 4, 0, tzinfo=UTC)


def _pair(backend: Any, scope: Scope) -> tuple[Any, Any]:
    """An older home and the newer one that will replace it."""
    return (
        _memory_for(backend, scope, "The user lives in Brighton."),
        _memory_for(backend, scope, "The user lives in Bath."),
    )


def test_close_memory_sets_the_close_fields_and_keeps_the_memory(backend) -> None:
    scope = _unique_scope()
    old, new = _pair(backend, scope)
    assert old.valid_until is None and old.replaced_by_id is None

    assert backend.close_memory(scope, old.id, valid_until=_CLOSED_AT, replaced_by_id=new.id) is True

    stored = backend.get_memory(scope, old.id)
    assert stored.valid_until == _CLOSED_AT and stored.replaced_by_id == new.id
    assert stored.content == old.content and stored.superseded_by_id is None  # not superseded, not deleted
    assert stored.id in {m.id for m in backend.list_memories(scope)}  # still an active memory
    assert backend.get_memory(scope, new.id).valid_until is None  # the newer one is untouched


def test_close_memory_is_idempotent_and_never_reopens_or_rewrites(backend) -> None:
    scope = _unique_scope()
    old, new = _pair(backend, scope)
    third = _memory_for(backend, scope, "The user lives in Leeds.")
    assert backend.close_memory(scope, old.id, valid_until=_CLOSED_AT, replaced_by_id=new.id) is True
    again = backend.close_memory(scope, old.id, valid_until=_CLOSED_AT.replace(day=29), replaced_by_id=third.id)
    assert again is False  # already closed: nothing changes, so the first answer stands
    stored = backend.get_memory(scope, old.id)
    assert stored.valid_until == _CLOSED_AT and stored.replaced_by_id == new.id


def test_close_memory_respects_scope(backend) -> None:
    scope, other = _unique_scope(), _unique_scope()
    old, new = _pair(backend, scope)
    assert backend.close_memory(other, old.id, valid_until=_CLOSED_AT, replaced_by_id=new.id) is False
    assert backend.get_memory(scope, old.id).valid_until is None


def test_close_memory_unknown_id_returns_false(backend) -> None:
    scope = _unique_scope()
    assert backend.close_memory(scope, "no-such-memory", valid_until=_CLOSED_AT, replaced_by_id="x") is False


def test_close_memory_skips_a_superseded_memory(backend) -> None:
    scope = _unique_scope()
    observation = backend.add_observation(Observation(scope=scope, content="obs"))
    old = backend.add_memory(
        Memory(
            scope=scope,
            kind="fact",
            content="first",
            confidence=0.9,
            source_observation_ids=[observation.id],
            model_id="m",
            prompt_version="v",
        )
    )
    backend.supersede_memories_for_source(scope, observation.id)
    assert backend.close_memory(scope, old.id, valid_until=_CLOSED_AT, replaced_by_id="x") is False


def test_close_memory_survives_a_usage_update(backend) -> None:
    if isinstance(backend, PostgreSQLBackend):
        pytest.skip(
            "record_memory_usage is broken on PostgreSQL on the base branch (jsonb_set is given a "
            "VARCHAR path and an integer); not caused by close_memory, tracked as a separate finding"
        )
    scope = _unique_scope()
    old, new = _pair(backend, scope)
    backend.close_memory(scope, old.id, valid_until=_CLOSED_AT, replaced_by_id=new.id)
    backend.record_memory_usage(scope, old.id)  # a read-modify-write elsewhere must not drop the close
    stored = backend.get_memory(scope, old.id)
    assert stored.valid_until == _CLOSED_AT and stored.replaced_by_id == new.id
    assert stored.usage_count == 1


def test_task_notes_conform_on_both_backends(backend):
    """One current note per task, history kept in order, scoped, clearable (task progress)."""
    scope, other = _scope(), _scope(profile_id="p2")
    assert backend.get_task_note(scope, "hps") == (None, [])
    backend.set_task_note(scope, "hps", "first", {"n": 1})
    backend.set_task_note(scope, "hps", "second", {"n": 2})
    backend.set_task_note(scope, "other", "x", {})
    current, history = backend.get_task_note(scope, "hps")
    assert current is not None and (current.state, current.metadata) == ("second", {"n": 2})
    assert [n.state for n in history] == ["first"] and history[0].replaced_at is not None
    assert [n.task for n in backend.list_task_notes(scope)] == ["other", "hps"]
    assert backend.list_task_notes(other) == [] and backend.get_task_note(other, "hps") == (None, [])
    assert backend.clear_task_note(scope, "hps") is True
    assert backend.clear_task_note(scope, "hps") is False
    assert [n.task for n in backend.list_task_notes(scope)] == ["other"]
    assert [n.state for n in backend.get_task_note(scope, "hps")[1]] == ["first"]


# --- M0: snapshot round-trip parity + cross-backend restore (plan v2 M0; REV MG-1) ---

_M0_QUERIES = ("Alice works at", "deploy window", "Globex", "Acme", "Alice")


def _m0_shape(bundle: Any, scope: Scope) -> None:
    """Production-shaped store: derive relations (one ended by replacement),
    a corrected-then-erased observation, task-note history, a manual dated relation."""
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    service = bundle.service
    service.ingest_observation(scope, content="Alice works at Acme")
    worker.drain(gateway=bundle.gateway)
    service.ingest_observation(scope, content="Alice works at Globex")
    worker.drain(gateway=bundle.gateway)
    view = service.ingest_observation(scope, content="The deploy window is Tuesday")
    worker.drain(gateway=bundle.gateway)
    revision = service.revise_observation(scope, view.observation_id, "The deploy window is Thursday")
    worker.drain(gateway=bundle.gateway)
    service.erase_observation(scope, revision.id)
    worker.drain(gateway=bundle.gateway)
    service.set_task_note(scope, task="m0-round-trip", state="first state", metadata={"step": 1})
    service.set_task_note(scope, task="m0-round-trip", state="second state", metadata={"step": 2})
    active = [
        m for m in bundle.repository.list_memories(scope)
        if m.superseded_by_id is None and m.valid_until is None
    ]
    if len(active) >= 2:
        bundle.repository.close_memory(
            scope, active[0].id, valid_until=datetime(2026, 3, 1, tzinfo=UTC), replaced_by_id=active[1].id
        )
    alice = bundle.repository.add_entity(scope, "person", "M0 Manual Alice")
    org = bundle.repository.add_entity(scope, "org", "M0 Manual Org")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    bundle.repository.add_relation(
        scope, alice.id, "visited", org.id, valid_from=start, valid_to=start + timedelta(days=1)
    )


def _m0_build(backend: str, tmp_path: Path, tag: str, *, target: bool = False) -> Any:
    if backend == "sqlite":
        return build_bundle(
            database=tmp_path / f"{tag}.db",
            fts=tmp_path / f"{tag}.fts",
            vectors=tmp_path / f"{tag}.vec",
        )
    dsn = PG_DSN.rsplit("/", 1)[0] + "/severian_dst" if target else PG_DSN
    return build_bundle(backend="postgresql", dsn=dsn)


@pytest.mark.skipif(not PG_DSN, reason="SEVERIAN_PG_DSN not set; live PostgreSQL lane")
@pytest.mark.parametrize(
    ("source_backend", "target_backend"),
    [("sqlite", "postgresql"), ("postgresql", "sqlite"), ("postgresql", "postgresql")],
)
def test_snapshot_round_trip_cross_backend(tmp_path, source_backend, target_backend) -> None:
    unique = uuid.uuid4().hex[:10]
    tenant, collection = f"m0x{unique}", f"m0xc{unique}"
    scope = Scope(
        tenant_id=tenant, profile_id="p1", collection_id=collection, agent_id="a1", session_id="s1"
    )
    source = _m0_build(source_backend, tmp_path, "src")
    source.repository.create_collection(Collection(id=collection, tenant_id=tenant, name="M0X"))
    _m0_shape(source, scope)
    exported = source.repository.export_json()
    # Round-trip only this test's tenant: the live PG source/target databases
    # are shared across params, so foreign records must not ride along.
    doc = json.loads(exported)
    doc["records"] = [r for r in doc["records"] if r["tenant_id"] == tenant]
    doc["checksum"] = source.repository.export_checksum(doc["records"])
    exported = json.dumps(doc, sort_keys=True)
    before = [
        [hit.memory_id for hit in source.service.search(scope, query=q, limit=10)]
        for q in _M0_QUERIES
    ]
    source.close()

    target = _m0_build(target_backend, tmp_path, "dst", target=True)
    target.repository.import_json(exported)
    target.projection.rebuild(scope)
    after = [
        [hit.memory_id for hit in target.service.search(scope, query=q, limit=10)]
        for q in _M0_QUERIES
    ]
    assert after == before
    src_records = {
        r["id"]: r for r in json.loads(exported)["records"] if r["tenant_id"] == tenant
    }
    dst_records = {
        r["id"]: r
        for r in json.loads(target.repository.export_json())["records"]
        if r["tenant_id"] == tenant
    }
    assert dst_records == src_records
    target.close()


# --- M2: mutation audit coverage (plan v2 M2; REV A2, MG-4) ------------------
#
# Every payload-mutating canonical write must leave exactly one append-only
# audit row per affected target, inside the same transaction, with ids/time in
# metadata and no new outbox event. ``record_memory_usage`` is exempt (read
# path, dormant in production); task notes are out of scope (working state).
# ``supersede_memory`` must stop writing a stale read-back copy.

_M2_CLOSED_AT = datetime(2026, 2, 3, 9, 30, tzinfo=UTC)

_NEW_OPERATIONS = ("supersede", "close", "source_superseded", "retract", "unsupport", "reopen")


def _audit_rows(backend: Any, scope: Scope, action: str | None = None) -> list[AuditEvent]:
    rows = backend.list_audit(scope)
    return rows if action is None else [row for row in rows if row.action == action]


def _outbox_ids(backend: Any) -> set[str]:
    return {item.id for item in backend.pending_outbox()}


def _assert_no_new_outbox_events(backend: Any) -> None:
    """The audit-only helper never drives the projection: no outbox row carries these ops."""
    kinds = [item.kind for item in backend.pending_outbox()]
    assert [kind for kind in kinds if any(op in kind for op in _NEW_OPERATIONS)] == []


def _audit_round_trip(backend: Any, scope: Scope, tmp_path: Path, tag: str) -> None:
    """M0 rule: every newly audited path still exports and re-imports unchanged."""
    document = json.loads(backend.export_json())
    document["records"] = [row for row in document["records"] if row["tenant_id"] == scope.tenant_id]
    document["checksum"] = backend.export_checksum(document["records"])
    target = SQLiteRepository(tmp_path / f"m2-{tag}.db")
    target.initialize()
    try:
        target.import_json(json.dumps(document, sort_keys=True))
        want = [(row.action, row.target_id, row.metadata) for row in backend.list_audit(scope)]
        got = [(row.action, row.target_id, row.metadata) for row in target.list_audit(scope)]
        assert got == want
    finally:
        target.close()


def test_close_memory_writes_exactly_one_audit_row(backend, tmp_path) -> None:
    scope = _unique_scope()
    old, new = _pair(backend, scope)
    before = _outbox_ids(backend)

    assert backend.close_memory(scope, old.id, valid_until=_M2_CLOSED_AT, replaced_by_id=new.id) is True

    rows = _audit_rows(backend, scope, "memory.close")
    assert [row.target_id for row in rows] == [old.id]
    assert rows[0].metadata["replaced_by_id"] == new.id
    # New datetimes in the payload use pydantic's JSON encoding (Z), not isoformat(+00:00).
    assert rows[0].metadata["valid_until"] == "2026-02-03T09:30:00Z"
    assert _outbox_ids(backend) == before
    _assert_no_new_outbox_events(backend)
    _audit_round_trip(backend, scope, tmp_path, "close")


def test_close_memory_audits_nothing_when_it_changes_nothing(backend, tmp_path) -> None:
    scope = _unique_scope()
    old, new = _pair(backend, scope)
    assert backend.close_memory(scope, old.id, valid_until=_M2_CLOSED_AT, replaced_by_id=new.id) is True

    assert backend.close_memory(scope, old.id, valid_until=_M2_CLOSED_AT, replaced_by_id=new.id) is False
    assert backend.close_memory(scope, "no-such-memory", valid_until=_M2_CLOSED_AT, replaced_by_id="x") is False
    assert backend.close_memory(_unique_scope(), old.id, valid_until=_M2_CLOSED_AT, replaced_by_id="x") is False

    assert len(_audit_rows(backend, scope, "memory.close")) == 1
    _audit_round_trip(backend, scope, tmp_path, "close-noop")


def test_supersede_memory_writes_one_audit_row_and_refuses_a_second(backend, tmp_path) -> None:
    scope = _unique_scope()
    keeper = _memory_for(backend, scope, "The user prefers tea.")
    loser = _memory_for(backend, scope, "The user prefers coffee.")
    before = _outbox_ids(backend)

    updated = backend.supersede_memory(scope, loser.id, keeper.id)
    assert updated.superseded_by_id == keeper.id

    rows = _audit_rows(backend, scope, "memory.supersede")
    assert [row.target_id for row in rows] == [loser.id]
    assert rows[0].metadata["replacement_id"] == keeper.id

    with pytest.raises(ConflictError):
        backend.supersede_memory(scope, loser.id, keeper.id)  # already superseded
    with pytest.raises(ConflictError):
        backend.supersede_memory(scope, keeper.id, keeper.id)  # itself
    assert len(_audit_rows(backend, scope, "memory.supersede")) == 1

    assert _outbox_ids(backend) == before
    _assert_no_new_outbox_events(backend)
    _audit_round_trip(backend, scope, tmp_path, "supersede")


def test_supersede_memory_refuses_a_foreign_scope_and_an_absent_id_with_conflict(backend) -> None:
    """One conditional UPDATE, so one outcome: nothing matched is ``ConflictError``.

    Owner rule 20/11 (supersedes the old "absent ⇒ ``NotFoundError``" port
    convention): the write is a single rowcount-guarded UPDATE, and ``rowcount
    == 0`` means the target is not an active memory in this scope — absent,
    foreign scope, erased or already superseded. Every one of those surfaces as
    ``ConflictError`` on BOTH backends, with no diagnostic read to classify it
    and nothing written.
    """
    scope, other = _unique_scope(), _unique_scope()
    keeper = _memory_for(backend, scope, "The user prefers tea.")
    loser = _memory_for(backend, scope, "The user prefers coffee.")

    with pytest.raises(ConflictError):
        backend.supersede_memory(other, loser.id, keeper.id)  # foreign scope
    with pytest.raises(ConflictError):
        backend.supersede_memory(scope, "no-such-memory", keeper.id)  # absent
    with pytest.raises(ConflictError):
        backend.supersede_memory(scope, loser.id, loser.id)  # itself

    assert backend.get_memory(scope, loser.id).superseded_by_id is None
    assert backend.get_memory(scope, keeper.id).superseded_by_id is None
    assert _audit_rows(backend, other) == []
    assert _audit_rows(backend, scope, "memory.supersede") == []


def _sibling_handle(backend: Any) -> Any:
    """A second, independent handle on the same store (a genuine concurrent writer)."""
    if isinstance(backend, PostgreSQLBackend):
        return PostgreSQLBackend(dsn=PG_DSN)
    return SQLiteRepository(backend.path)


def test_supersede_memory_does_not_lose_a_concurrent_close(backend, tmp_path) -> None:
    """MG-4 lost update: a close landing between the read and the write must survive.

    The close is committed on a SEPARATE connection the moment supersede_memory is
    about to issue its UPDATE — the deterministic interleaving that a stale read-back
    copy loses. The fix writes one conditional UPDATE of the CURRENT payload, so the
    close's ``valid_until``/``replaced_by_id`` survive.
    """
    scope = _unique_scope()
    keeper = _memory_for(backend, scope, "The user prefers tea.")
    loser = _memory_for(backend, scope, "The user prefers coffee.")
    closer = _sibling_handle(backend)
    interleaved: list[str] = []

    def close_first(_connection, _cursor, statement, _parameters, _context, _many):
        if interleaved or not statement.lstrip().upper().startswith("UPDATE RECORDS"):
            return
        interleaved.append(statement.split()[0])
        closer.close_memory(scope, loser.id, valid_until=_M2_CLOSED_AT, replaced_by_id=keeper.id)

    event.listen(backend.engine, "before_cursor_execute", close_first)
    try:
        backend.supersede_memory(scope, loser.id, keeper.id)
    finally:
        event.remove(backend.engine, "before_cursor_execute", close_first)
        closer.close()

    assert interleaved, "the concurrent close must interleave inside supersede_memory"
    stored = backend.get_memory(scope, loser.id)
    assert stored.superseded_by_id == keeper.id  # the supersede landed
    assert stored.valid_until == _M2_CLOSED_AT  # ... and the close was not lost
    assert stored.replaced_by_id == keeper.id
    _audit_round_trip(backend, scope, tmp_path, "race")


def test_supersede_memories_for_source_writes_one_row_per_memory(backend, tmp_path) -> None:
    scope = _unique_scope()
    observation = backend.add_observation(Observation(scope=scope, content="two facts, one source"))
    first = backend.add_memory(
        Memory(
            scope=scope,
            kind="fact",
            content="first derived fact",
            confidence=0.9,
            source_observation_ids=[observation.id],
            model_id="m",
            prompt_version="v",
        )
    )
    second = backend.add_memory(
        Memory(
            scope=scope,
            kind="fact",
            content="second derived fact",
            confidence=0.9,
            source_observation_ids=[observation.id],
            model_id="m",
            prompt_version="v",
        )
    )
    untouched = _memory_for(backend, scope, "a fact with a different source")

    assert backend.supersede_memories_for_source(scope, observation.id) == 2

    rows = _audit_rows(backend, scope, "memory.source_superseded")
    assert sorted(row.target_id for row in rows) == sorted([first.id, second.id])
    assert {row.metadata["observation_id"] for row in rows} == {observation.id}
    assert untouched.id not in {row.target_id for row in rows}

    # Idempotent: a second pass changes nothing, so it audits nothing.
    assert backend.supersede_memories_for_source(scope, observation.id) == 0
    assert len(_audit_rows(backend, scope, "memory.source_superseded")) == 2
    _assert_no_new_outbox_events(backend)
    _audit_round_trip(backend, scope, tmp_path, "source-superseded")


def test_retract_relations_for_source_audits_unsupport_then_retract(backend, tmp_path) -> None:
    scope = _unique_scope()
    first_source = backend.add_observation(Observation(scope=scope, content="Alice works at Acme"))
    second_source = backend.add_observation(Observation(scope=scope, content="Acme says Alice is here"))
    for source in (first_source, second_source):
        backend.publish_derivation(
            scope,
            entities=[("person", "Alice"), ("organisation", "Acme")],
            relations=[("Alice", "works_at", "Acme")],
            memories=[],
            source_observation_id=source.id,
        )
    relations = [rel for rel in backend.list_relations(scope) if rel.predicate == "works_at"]
    assert len(relations) == 1
    relation = relations[0]
    assert set(relation.source_observation_ids) == {first_source.id, second_source.id}

    # One support lost, another remains: unsupported, not retracted.
    backend.retract_relations_for_source(scope, first_source.id)
    unsupported = _audit_rows(backend, scope, "relation.unsupport")
    assert [row.target_id for row in unsupported] == [relation.id]
    assert unsupported[0].metadata["observation_id"] == first_source.id
    assert unsupported[0].metadata["retracted"] is False

    # Last support lost: retracted.
    backend.retract_relations_for_source(scope, second_source.id)
    retracted = _audit_rows(backend, scope, "relation.retract")
    assert [row.target_id for row in retracted] == [relation.id]
    assert retracted[0].metadata["observation_id"] == second_source.id
    assert retracted[0].metadata["retracted"] is True

    _assert_no_new_outbox_events(backend)
    _audit_round_trip(backend, scope, tmp_path, "relation-retract")


def test_publish_derivation_audits_a_supported_relation_and_its_ending(backend, tmp_path) -> None:
    scope = _unique_scope()
    first = backend.add_observation(Observation(scope=scope, content="Alice works at Acme"))
    backend.publish_derivation(
        scope,
        entities=[("person", "Alice"), ("organisation", "Acme")],
        relations=[("Alice", "works_at", "Acme")],
        memories=[],
        source_observation_id=first.id,
    )
    acme = next(rel for rel in backend.list_relations(scope) if rel.predicate == "works_at")

    second = backend.add_observation(Observation(scope=scope, content="Acme confirms Alice"))
    backend.publish_derivation(
        scope,
        entities=[("person", "Alice"), ("organisation", "Acme")],
        relations=[("Alice", "works_at", "Acme")],
        memories=[],
        source_observation_id=second.id,
    )
    supported = _audit_rows(backend, scope, "relation.support")
    assert [row.target_id for row in supported] == [acme.id]
    assert supported[0].metadata["source_observation_id"] == second.id

    # A newer single-valued statement ends the old value: its end is audited.
    third = backend.add_observation(Observation(scope=scope, content="Alice works at Globex"))
    backend.publish_derivation(
        scope,
        entities=[("person", "Alice"), ("organisation", "Globex")],
        relations=[("Alice", "works_at", "Globex")],
        memories=[],
        source_observation_id=third.id,
    )
    globex = next(
        rel
        for rel in backend.list_relations(scope)
        if rel.predicate == "works_at" and rel.object_id != acme.object_id
    )
    ended = _audit_rows(backend, scope, "relation.end")
    assert [row.target_id for row in ended] == [acme.id]
    assert ended[0].metadata["replaced_by_id"] == globex.id
    assert str(ended[0].metadata["valid_to"]).endswith("Z")

    _assert_no_new_outbox_events(backend)
    _audit_round_trip(backend, scope, tmp_path, "relation-end")


def test_publish_derivation_audits_reopening_a_restated_relation(backend, tmp_path) -> None:
    scope = _unique_scope()

    def publish(content: str, subject: str, obj: str) -> None:
        observation = backend.add_observation(Observation(scope=scope, content=content))
        backend.publish_derivation(
            scope,
            entities=[("person", subject), ("organisation", obj)],
            relations=[(subject, "works_at", obj)],
            memories=[],
            source_observation_id=observation.id,
        )

    publish("Alice works at Acme", "Alice", "Acme")
    acme = next(rel for rel in backend.list_relations(scope) if rel.predicate == "works_at")
    publish("Alice works at Globex", "Alice", "Globex")  # the Acme relation is ended
    publish("Alice is back at Acme", "Alice", "Acme")  # ... and restated: reopened

    reopened = _audit_rows(backend, scope, "relation.reopen")
    assert [row.target_id for row in reopened] == [acme.id]
    open_values = [rel for rel in backend.list_relations(scope) if rel.predicate == "works_at"]
    assert [(rel.object_id, rel.valid_to) for rel in open_values if rel.id == acme.id] == [
        (acme.object_id, None)
    ]
    _audit_round_trip(backend, scope, tmp_path, "relation-reopen")


def _payload_action(payload: Any) -> str | None:
    """The audit action inside a bound payload, whatever the driver wraps it in.

    SQLite binds the JSON column as text; psycopg binds it as a ``Jsonb`` wrapper
    whose ``repr`` is truncated, so the value must be unwrapped, never string-matched.
    """
    for candidate in (payload, getattr(payload, "obj", None), getattr(payload, "adapted", None)):
        if isinstance(candidate, dict):
            return candidate.get("action")
        if isinstance(candidate, (str, bytes)):
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed.get("action")
    return None


def _is_audit_insert(statement: str, parameters: Any, action: str | None = None) -> bool:
    """True for the INSERT of an audit record (optionally of one specific action)."""
    if not statement.lstrip().upper().startswith("INSERT INTO RECORDS"):
        return False
    if isinstance(parameters, dict):
        if parameters.get("record_type") != "audit":
            return False
        return action is None or _payload_action(parameters.get("payload")) == action
    try:
        values = list(parameters)
    except TypeError:  # pragma: no cover - unexpected driver shape
        return False
    if len(values) < 2 or values[1] != "audit":
        return False
    return action is None or action in str(parameters)


def test_close_memory_and_its_audit_row_commit_together(backend) -> None:
    """Same transaction: if the audit row cannot be written, the close does not happen."""
    scope = _unique_scope()
    old, new = _pair(backend, scope)

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if _is_audit_insert(statement, parameters, "memory.close"):
            raise RuntimeError("injected audit failure")

    event.listen(backend.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            backend.close_memory(scope, old.id, valid_until=_M2_CLOSED_AT, replaced_by_id=new.id)
    finally:
        event.remove(backend.engine, "before_cursor_execute", reject)

    stored = backend.get_memory(scope, old.id)
    assert stored.valid_until is None and stored.replaced_by_id is None
    assert _audit_rows(backend, scope, "memory.close") == []


def test_supersede_memory_and_its_audit_row_commit_together(backend) -> None:
    scope = _unique_scope()
    keeper = _memory_for(backend, scope, "The user prefers tea.")
    loser = _memory_for(backend, scope, "The user prefers coffee.")

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if _is_audit_insert(statement, parameters, "memory.supersede"):
            raise RuntimeError("injected audit failure")

    event.listen(backend.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            backend.supersede_memory(scope, loser.id, keeper.id)
    finally:
        event.remove(backend.engine, "before_cursor_execute", reject)

    assert backend.get_memory(scope, loser.id).superseded_by_id is None
    assert _audit_rows(backend, scope, "memory.supersede") == []


def test_relation_end_and_its_audit_row_commit_with_the_publish(backend) -> None:
    scope = _unique_scope()
    backend.publish_derivation(
        scope,
        entities=[("person", "Alice"), ("organisation", "Acme")],
        relations=[("Alice", "works_at", "Acme")],
        memories=[],
        source_observation_id=backend.add_observation(
            Observation(scope=scope, content="Alice works at Acme")
        ).id,
    )

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if _is_audit_insert(statement, parameters, "relation.end"):
            raise RuntimeError("injected audit failure")

    event.listen(backend.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            backend.publish_derivation(
                scope,
                entities=[("person", "Alice"), ("organisation", "Globex")],
                relations=[("Alice", "works_at", "Globex")],
                memories=[],
                source_observation_id=backend.add_observation(
                    Observation(scope=scope, content="Alice works at Globex")
                ).id,
            )
    finally:
        event.remove(backend.engine, "before_cursor_execute", reject)

    # The whole publish rolled back: the old value is still the open one.
    assert _open_objects(backend, scope, "works_at") == {"Acme"}
    assert _audit_rows(backend, scope, "relation.end") == []


def _curation_corpus(backend: Any, scope: Scope) -> tuple[Memory, Memory]:
    """A fresh memory and a stale low-confidence one, so one pass scores both."""
    fresh = _memory_for(backend, scope, "The user likes tea with breakfast.")
    stale = backend.add_memory(
        Memory(
            scope=scope,
            kind="fact",
            content="The user once mentioned a hobby.",
            confidence=0.05,
            source_observation_ids=list(fresh.source_observation_ids),
            model_id="m",
            prompt_version="v",
            created_at=datetime.now(UTC) - timedelta(days=400),
        )
    )
    return fresh, stale


def test_curation_report_reference_is_audited_on_both_backends(backend, tmp_path) -> None:
    """The marker claims "review proposals in audit"; the reference must persist."""
    scope = _unique_scope()
    fresh, stale = _curation_corpus(backend, scope)

    report = CurationService(backend).curate(scope)

    assert report.review_marker_ids
    rows = _audit_rows(backend, scope, "curation.report")
    assert [row.target_id for row in rows] == report.review_marker_ids
    metadata = rows[0].metadata
    assert metadata["report_id"] == report.report_id
    # The marker is named ONCE, by the row's target_id: a second copy of the id in
    # metadata could disagree with the row it rides on (an identical marker
    # already in the scope is reused, and the write decides which one it names).
    assert "review_marker_id" not in metadata
    assert metadata["memories_scored"] >= 2
    assert metadata["low_value_candidates"] == len(metadata["low_value_ids"])
    assert stale.id in metadata["low_value_ids"]
    assert fresh.id not in metadata["low_value_ids"]
    assert metadata["contradiction_pairs"] == len(metadata["contradiction_ids"])
    _audit_round_trip(backend, scope, tmp_path, "curation")


def test_add_memory_with_audit_writes_one_memory_and_one_audit_row(backend, tmp_path) -> None:
    """The combined write: ``add_memory``'s creation, plus ONE audit row, atomically."""
    scope = _unique_scope()
    observation = backend.add_observation(Observation(scope=scope, content="the user prefers tea"))
    memory = Memory(
        scope=scope,
        kind="curation_review",
        content="Curation review: 2 memories scored, 0 low-value candidates.",
        source_observation_ids=[observation.id],
        model_id="m",
        prompt_version="v",
        confidence=1.0,
    )
    before = _outbox_ids(backend)

    stored, row = backend.add_memory_with_audit(
        memory,
        action="curation.report",
        metadata_={"report_id": "r-1", "observed_at": datetime(2026, 2, 3, 9, 30, tzinfo=UTC)},
    )

    assert stored.id == memory.id
    assert backend.get_memory(scope, memory.id).content == memory.content
    assert (row.action, row.target_id) == ("curation.report", memory.id)
    # The RETURNED event carries the caller's own values. What is STORED goes
    # through pydantic's JSON encoding, so a datetime lands as the Z form
    # ("2026-02-03T09:30:00Z"), like every other new payload field, and the
    # export/import round-trip keeps it intact.
    assert row.metadata["observed_at"] == datetime(2026, 2, 3, 9, 30, tzinfo=UTC)
    assert [(r.action, r.target_id, r.metadata) for r in _audit_rows(backend, scope, "curation.report")] == [
        ("curation.report", memory.id, {"report_id": "r-1", "observed_at": "2026-02-03T09:30:00Z"})
    ]

    # add_memory's creation semantics are kept: exactly one memory.created outbox
    # event, which is what drives the projection.
    new_events = [item for item in backend.pending_outbox() if item.id not in before]
    assert [(item.kind, item.payload) for item in new_events] == [("memory.created", {"id": memory.id})]

    # A repeat call dedupes onto the stored marker (one memory) and still lands its
    # own reference row, naming the memory that is actually stored.
    again, second = backend.add_memory_with_audit(memory, action="curation.report", metadata_={"report_id": "r-2"})
    assert again.id == memory.id and second.target_id == memory.id
    assert len([m for m in backend.list_memories(scope) if m.kind == "curation_review"]) == 1
    assert len(_audit_rows(backend, scope, "curation.report")) == 2
    _audit_round_trip(backend, scope, tmp_path, "memory-with-audit")


def test_add_memory_with_audit_and_its_audit_row_commit_together(backend) -> None:
    """Same transaction: no memory, no outbox event and no audit row when either write fails."""
    scope = _unique_scope()
    observation = backend.add_observation(Observation(scope=scope, content="the user prefers tea"))
    memory = Memory(
        scope=scope,
        kind="curation_review",
        content="Curation review: 2 memories scored, 0 low-value candidates.",
        source_observation_ids=[observation.id],
        model_id="m",
        prompt_version="v",
        confidence=1.0,
    )
    before = _outbox_ids(backend)

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if _is_audit_insert(statement, parameters, "curation.report"):
            raise RuntimeError("injected audit failure")

    event.listen(backend.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            backend.add_memory_with_audit(memory, action="curation.report", metadata_={"report_id": "r-1"})
    finally:
        event.remove(backend.engine, "before_cursor_execute", reject)

    assert [m for m in backend.list_memories(scope) if m.kind == "curation_review"] == []
    assert _audit_rows(backend, scope, "curation.report") == []
    assert _outbox_ids(backend) == before


def test_curation_marker_and_its_report_reference_commit_together(backend) -> None:
    """The marker's "review proposals in audit" claim is written in ONE transaction.

    The marker used to be ``add_memory`` (+ its ``memory.created`` outbox event)
    followed by a separate ``append_audit``. A failure on the second write left a
    marker promising evidence it does not have, so the claim was untrue exactly
    when it mattered. Injecting the failure on the reference INSERT must now undo
    the marker, its outbox event and the reference together.
    """
    scope = _unique_scope()
    _curation_corpus(backend, scope)
    before = _outbox_ids(backend)

    def reject(_connection, _cursor, statement, parameters, _context, _many):
        if _is_audit_insert(statement, parameters, "curation.report"):
            raise RuntimeError("injected audit failure")

    event.listen(backend.engine, "before_cursor_execute", reject)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            CurationService(backend).curate(scope)
    finally:
        event.remove(backend.engine, "before_cursor_execute", reject)

    assert [m for m in backend.list_memories(scope) if m.kind == "curation_review"] == []
    assert _audit_rows(backend, scope, "curation.report") == []
    assert _outbox_ids(backend) == before


def test_the_curation_pass_creates_its_marker_the_normal_way(backend, tmp_path) -> None:
    """Success shape: one marker, one reference, and ``add_memory``'s outbox behaviour kept."""
    scope = _unique_scope()
    fresh, stale = _curation_corpus(backend, scope)
    before = _outbox_ids(backend)

    report = CurationService(backend).curate(scope)

    markers = [m for m in backend.list_memories(scope) if m.kind == "curation_review"]
    assert [m.id for m in markers] == report.review_marker_ids
    rows = _audit_rows(backend, scope, "curation.report")
    assert [row.target_id for row in rows] == [markers[0].id]
    assert rows[0].metadata == report.reference()
    assert rows[0].metadata["report_id"] == report.report_id
    assert stale.id in rows[0].metadata["low_value_ids"]

    # Exactly one new outbox event: the marker's own creation. The reference adds none.
    new_events = [item for item in backend.pending_outbox() if item.id not in before]
    assert [(item.kind, item.payload) for item in new_events] == [("memory.created", {"id": markers[0].id})]
    assert [item.kind for item in backend.pending_outbox() if "curation" in item.kind] == []
    _audit_round_trip(backend, scope, tmp_path, "curation-atomic")


def test_audit_rows_stay_append_only(backend) -> None:
    scope = _unique_scope()
    old, new = _pair(backend, scope)
    backend.close_memory(scope, old.id, valid_until=_M2_CLOSED_AT, replaced_by_id=new.id)
    first = {(row.id, row.action, row.target_id) for row in backend.list_audit(scope)}
    assert first

    keeper = _memory_for(backend, scope, "The user prefers tea.")
    loser = _memory_for(backend, scope, "The user prefers coffee.")
    backend.supersede_memory(scope, loser.id, keeper.id)

    after = {(row.id, row.action, row.target_id) for row in backend.list_audit(scope)}
    assert first <= after
    assert len(after) > len(first)
    assert [row.action for row in backend.list_audit(scope) if "audit." in row.action] == []

