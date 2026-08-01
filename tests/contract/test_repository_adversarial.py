from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import event

from severian.domain.models import Collection, Memory, Observation, Relation, Scope
from severian.infrastructure.sqlite import ConflictError, NotFoundError, SQLiteRepository


def scope(tenant: str = "t1", profile: str = "p1") -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id=profile,
        collection_id="c1",
        agent_id="a1",
        session_id="s1",
    )


@pytest.fixture
def repo(tmp_path):
    value = SQLiteRepository(tmp_path / "severian.db")
    value.initialize()
    yield value
    value.close()


def seed_collection(repo: SQLiteRepository, tenant: str = "t1") -> None:
    repo.create_collection(Collection(id=f"c-{tenant}", tenant_id=tenant, name=f"Collection {tenant}"))


def test_direct_reads_require_exact_scope(repo):
    seed_collection(repo)
    observation = repo.add_observation(Observation(scope=scope(), content="private"))
    memory = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="derived",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.9,
        )
    )

    assert repo.get_observation(scope(), observation.id) == observation
    assert repo.get_memory(scope(), memory.id) == memory
    with pytest.raises(NotFoundError):
        repo.get_observation(scope("t2"), observation.id)
    with pytest.raises(NotFoundError):
        repo.get_memory(scope("t2"), memory.id)


def test_memory_rejects_cross_scope_source(repo):
    seed_collection(repo)
    seed_collection(repo, "t2")
    source = repo.add_observation(Observation(scope=scope(), content="private"))

    with pytest.raises(ConflictError, match="scope"):
        repo.add_memory(
            Memory(
                scope=scope("t2"),
                kind="fact",
                content="leak",
                source_observation_ids=[source.id],
                model_id="local/model",
                prompt_version="v1",
                confidence=0.9,
            )
        )


def test_memory_revision_preserves_history_and_rejects_second_head(repo):
    seed_collection(repo)
    source = repo.add_observation(Observation(scope=scope(), content="evidence"))
    original = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="old",
            source_observation_ids=[source.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )

    revision = repo.revise_memory(scope(), original.id, content="new", model_id="local/model-2", prompt_version="v2")
    assert revision.revision_of_id == original.id
    assert repo.get_memory(scope(), original.id).content == "old"
    assert repo.get_memory(scope(), original.id).superseded_by_id == revision.id
    with pytest.raises(ConflictError, match="already superseded"):
        repo.revise_memory(scope(), original.id, content="another", model_id="local/model-3", prompt_version="v3")


def test_revision_events_and_audit_actions_are_semantically_distinct(repo):
    seed_collection(repo)
    observation = repo.add_observation(Observation(scope=scope(), content="old evidence"))
    memory = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="old fact",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    observation_revision = repo.revise_observation(scope(), observation.id, "new evidence")
    memory_revision = repo.revise_memory(
        scope(), memory.id, content="new fact", model_id="local/model-2", prompt_version="v2"
    )

    events = {(event.payload["id"], event.kind) for event in repo.pending_outbox()}
    audits = {(event.target_id, event.action) for event in repo.list_audit(scope())}
    assert (observation_revision.id, "observation.revised") in events
    assert (memory_revision.id, "memory.revised") in events
    assert (observation_revision.id, "observation.revise") in audits
    assert (memory_revision.id, "memory.revise") in audits


def test_entity_dedup_and_relation_integrity(repo):
    seed_collection(repo)
    person = repo.add_entity(scope(), "person", "Ada")
    assert repo.add_entity(scope(), "person", "Ada").id == person.id

    with pytest.raises(NotFoundError):
        repo.add_relation(scope(), person.id, "knows", "missing")
    other_scope = repo.add_entity(scope("t2"), "person", "Other")
    with pytest.raises(ConflictError, match="scope"):
        repo.add_relation(scope(), person.id, "knows", other_scope.id)


def test_concurrent_relation_dedup_returns_one_relation(tmp_path):
    path = tmp_path / "severian.db"
    seed = SQLiteRepository(path)
    seed.initialize()
    seed_collection(seed)
    person = seed.add_entity(scope(), "person", "Ada")
    organisation = seed.add_entity(scope(), "organisation", "Engines")
    seed.close()

    def add_relation(_index: int) -> str:
        worker = SQLiteRepository(path)
        try:
            return worker.add_relation(scope(), person.id, "founded", organisation.id).id
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=10) as pool:
        relation_ids = list(pool.map(add_relation, range(10)))
    assert len(set(relation_ids)) == 1


def test_relation_rejects_inverted_temporal_interval():
    with pytest.raises(ValueError, match="valid_to"):
        Relation(
            scope=scope(),
            subject_id="a",
            predicate="knows",
            object_id="b",
            valid_from=datetime(2026, 1, 1, tzinfo=UTC),
            valid_to=datetime(2025, 1, 1, tzinfo=UTC),
        )


def test_job_claim_is_atomic_across_repository_instances(tmp_path):
    path = tmp_path / "severian.db"
    repo = SQLiteRepository(path)
    repo.initialize()
    seed_collection(repo)
    first = repo.enqueue_job(scope(), "derive", {"n": 1})
    second = repo.enqueue_job(scope(), "derive", {"n": 2})
    repo.close()

    def claim(worker: str) -> str:
        worker_repo = SQLiteRepository(path)
        try:
            return worker_repo.claim_job(worker).id
        finally:
            worker_repo.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(claim, ["w1", "w2"]))
    assert set(ids) == {first.id, second.id}


def test_canonical_write_emits_outbox_and_audit_atomically(repo):
    seed_collection(repo)
    observation = repo.add_observation(Observation(scope=scope(), content="evidence"))
    assert [(event.kind, event.payload["id"]) for event in repo.pending_outbox()] == [
        ("observation.created", observation.id)
    ]
    assert [(event.action, event.target_id) for event in repo.list_audit(scope())] == [
        ("observation.create", observation.id)
    ]


def test_canonical_write_rolls_back_when_audit_insert_fails(repo):
    seed_collection(repo)

    def reject_audit(_connection, _cursor, statement, parameters, _context, _many):
        if statement.startswith("INSERT INTO records") and parameters[1] == "audit":
            raise RuntimeError("injected audit failure")

    event.listen(repo.engine, "before_cursor_execute", reject_audit)
    try:
        with pytest.raises(RuntimeError, match="injected audit failure"):
            repo.add_observation(Observation(scope=scope(), content="must roll back"))
    finally:
        event.remove(repo.engine, "before_cursor_execute", reject_audit)

    assert repo.list_observations(scope()) == []
    assert repo.pending_outbox() == []
    assert repo.list_audit(scope()) == []


def test_import_rejects_conflict_and_checksum_tampering(repo):
    seed_collection(repo)
    observation = repo.add_observation(Observation(scope=scope(), content="original"))
    exported = json.loads(repo.export_json())

    tampered = dict(exported)
    tampered["checksum"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        repo.import_json(json.dumps(tampered))

    conflicting = json.loads(repo.export_json())
    for record in conflicting["records"]:
        if record["id"] == observation.id:
            # Forge the content coherently (payload content, payload
            # content_hash, column content_hash, identity key) so the
            # import passes semantic validation and is rejected at the
            # existing-record conflict check.
            digest = hashlib.sha256(b"changed").hexdigest()
            record["payload"]["content"] = "changed"
            record["payload"]["content_hash"] = digest
            record["content_hash"] = digest
            record["identity_key"] = SQLiteRepository._identity(digest)
    conflicting["checksum"] = SQLiteRepository.export_checksum(conflicting["records"])
    with pytest.raises(ConflictError, match="conflict"):
        repo.import_json(json.dumps(conflicting))
    assert repo.get_observation(scope(), observation.id).content == "original"


def test_import_rejects_scoped_identity_conflict_as_domain_error(repo):
    seed_collection(repo)
    repo.add_observation(Observation(scope=scope(), content="original"))
    exported = json.loads(repo.export_json())
    duplicate = next(record.copy() for record in exported["records"] if record["record_type"] == "observation")
    duplicate["id"] = "different-id"
    duplicate["payload"] = {**duplicate["payload"], "id": "different-id"}
    exported["records"].append(duplicate)
    exported["checksum"] = SQLiteRepository.export_checksum(exported["records"])

    with pytest.raises(ConflictError, match="scoped identity"):
        repo.import_json(json.dumps(exported))


def test_concurrent_import_never_leaks_database_errors(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    source.initialize()
    seed_collection(source)
    source.add_observation(Observation(scope=scope(), content="original"))
    exported = source.export_json()
    source.close()

    destination = tmp_path / "destination.db"
    initialized = SQLiteRepository(destination)
    initialized.initialize()
    initialized.close()

    def import_once(_index: int) -> str:
        worker = SQLiteRepository(destination)
        try:
            worker.import_json(exported)
            return "ok"
        except ConflictError:
            return "conflict"
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=5) as pool:
        outcomes = list(pool.map(import_once, range(5)))
    assert set(outcomes) <= {"ok", "conflict"}
    assert "ok" in outcomes


def test_collection_populates_tenant_and_collection_columns(repo):
    collection = repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    assert repo.get_collection("t1", collection.id) == collection
    with pytest.raises(NotFoundError):
        repo.get_collection("t2", collection.id)


def test_import_rejects_rechecksummed_scope_metadata_forgery(repo, tmp_path):
    seed_collection(repo)
    repo.add_observation(Observation(scope=scope(), content="private"))
    document = json.loads(repo.export_json())
    observation_record = next(item for item in document["records"] if item["record_type"] == "observation")
    observation_record["tenant_id"] = "attacker"
    document["checksum"] = SQLiteRepository.export_checksum(document["records"])

    restored = SQLiteRepository(tmp_path / "restored.db")
    restored.initialize()
    try:
        with pytest.raises(ValueError, match="payload metadata"):
            restored.import_json(json.dumps(document))
        assert restored.list_observations(scope()) == []
    finally:
        restored.close()
