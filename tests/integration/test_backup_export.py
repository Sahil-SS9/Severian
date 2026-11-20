import json
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from severian.application.message_units import message_unit_memories
from severian.application.worker import DerivationWorker
from severian.composition import build_bundle
from severian.domain.models import Collection, Observation, Scope
from severian.infrastructure.snapshot import validate_snapshot
from severian.infrastructure.sqlite import SQLiteRepository


def test_backup_restore_and_portable_export_import(tmp_path):
    source = SQLiteRepository(tmp_path / "source.db")
    source.initialize()
    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    source.create_collection(Collection(id="c", tenant_id="t", name="C"))
    source.add_observation(Observation(scope=scope, content="preserved", source={"uri": "x"}))
    backup = tmp_path / "backup.db"
    source.backup(backup)
    assert not (tmp_path / "backup.db-wal").exists()
    assert not (tmp_path / "backup.db-shm").exists()
    exported = source.export_json()
    source.close()

    restored = SQLiteRepository(backup)
    restored.initialize()
    assert restored.list_observations(scope)[0].content == "preserved"
    restored.close()

    imported = SQLiteRepository(tmp_path / "imported.db")
    imported.initialize()
    imported.import_json(exported)
    imported.import_json(exported)
    assert imported.list_observations(scope)[0].source == {"uri": "x"}
    imported.close()


def test_existing_0001_database_converges_to_scoped_identity_schema(tmp_path):
    database = tmp_path / "migration.db"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "0001")
    old_engine = create_engine(f"sqlite:///{database}")
    assert {item["name"] for item in inspect(old_engine).get_unique_constraints("records")} == {
        "uq_observation_content"
    }
    old_engine.dispose()

    command.upgrade(config, "head")
    upgraded_engine = create_engine(f"sqlite:///{database}")
    inspector = inspect(upgraded_engine)
    assert inspector.get_unique_constraints("records") == []
    indexes = {item["name"]: item for item in inspector.get_indexes("records")}
    assert indexes["uq_records_scoped_identity"]["unique"] == 1
    assert "identity_key" in {item["name"] for item in inspector.get_columns("records")}
    upgraded_engine.dispose()


# --- M0: snapshot round-trip on derive-shaped stores (plan v2 M0; REV MG-1) ---
# RED witnesses: today import_json rejects (a) derive-path relations (identity
# scheme mismatch), (b) any task_note record (unknown record_type), and
# (c) manual relations with validity set (datetime-vs-ISO identity encoding).

M0_QUERIES = ("Alice works at", "deploy window", "Globex", "Acme", "Alice")


def _m0_scope(tenant: str, collection: str) -> Scope:
    return Scope(tenant_id=tenant, profile_id="p1", collection_id=collection, agent_id="a1", session_id="s1")


def _m0_shape(bundle, scope) -> None:
    """Shape a store the way production does: derive-path relations (one ended
    by replacement), a corrected-then-erased observation, task-note history and
    a manual dated relation."""
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
    active = [m for m in bundle.repository.list_memories(scope) if m.superseded_by_id is None and m.valid_until is None]
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


def _m0_top10(bundle, scope):
    return [[hit.memory_id for hit in bundle.service.search(scope, query=q, limit=10)] for q in M0_QUERIES]


def _m0_records(document: str):
    return {record["id"]: record for record in json.loads(document)["records"]}


def test_derive_shaped_store_round_trips(tmp_path):
    source = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    scope = _m0_scope("m0t", "m0c")
    source.repository.create_collection(Collection(id="m0c", tenant_id="m0t", name="M0"))
    _m0_shape(source, scope)
    exported = source.repository.export_json()
    before = _m0_top10(source, scope)
    source.close()

    target = build_bundle(database=tmp_path / "i.db", fts=tmp_path / "i.fts", vectors=tmp_path / "i.vec")
    target.repository.import_json(exported)
    target.projection.rebuild(scope)
    assert _m0_top10(target, scope) == before
    assert _m0_records(target.repository.export_json()) == _m0_records(exported)
    target.close()


def test_message_unit_store_round_trips_and_keeps_order(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
    source = build_bundle(database=tmp_path / "u.db", fts=tmp_path / "u.fts", vectors=tmp_path / "u.vec")
    scope = _m0_scope("m0u", "m0cu")
    source.repository.create_collection(Collection(id="m0cu", tenant_id="m0u", name="M0 units"))
    observation = source.repository.add_observation(
        Observation(
            scope=scope,
            content="[2024/03/15 (Fri)] user: deadline April 1\nassistant: noted",
            source={"turn": 3},
        )
    )
    units = message_unit_memories(observation)
    assert len(units) == 2
    for unit in units:
        source.repository.add_memory(unit)
    exported = source.repository.export_json()
    before = [unit.content for unit in units]
    source.close()

    target = build_bundle(database=tmp_path / "ui.db", fts=tmp_path / "ui.fts", vectors=tmp_path / "ui.vec")
    target.repository.import_json(exported)
    target.projection.rebuild(scope)
    imported = [memory.content for memory in target.repository.list_memories(scope) if memory.kind == "message_unit"]
    assert imported == before
    target.close()


def test_task_note_records_round_trip(tmp_path):
    source = build_bundle(database=tmp_path / "n.db", fts=tmp_path / "n.fts", vectors=tmp_path / "n.vec")
    scope = _m0_scope("m0n", "m0nc")
    source.repository.create_collection(Collection(id="m0nc", tenant_id="m0n", name="M0 Notes"))
    source.service.ingest_observation(scope, content="M0 task note anchor")
    DerivationWorker(source.repository, source.derivation, source.projection).drain(gateway=source.gateway)
    source.service.set_task_note(scope, task="m0-note", state="v1", metadata={})
    source.service.set_task_note(scope, task="m0-note", state="v2", metadata={})
    source.service.clear_task_note(scope, task="m0-note")
    source.service.set_task_note(scope, task="m0-note", state="v3", metadata={"step": 3})
    exported = source.repository.export_json()
    source.close()

    target = build_bundle(database=tmp_path / "ni.db", fts=tmp_path / "ni.fts", vectors=tmp_path / "ni.vec")
    target.repository.import_json(exported)
    assert _m0_records(target.repository.export_json()) == _m0_records(exported)
    current, history = target.service.get_task_note(scope, task="m0-note")
    assert current is not None and current.state == "v3"
    assert [note.state for note in history] == ["v1"]
    target.close()


def test_manual_dated_relation_round_trips(tmp_path):
    source = build_bundle(database=tmp_path / "r.db", fts=tmp_path / "r.fts", vectors=tmp_path / "r.vec")
    scope = _m0_scope("m0r", "m0rc")
    source.repository.create_collection(Collection(id="m0rc", tenant_id="m0r", name="M0 Relations"))
    alice = source.repository.add_entity(scope, "person", "M0 Dated Ada")
    org = source.repository.add_entity(scope, "org", "M0 Dated Org")
    start = datetime(2026, 2, 1, tzinfo=UTC)
    source.repository.add_relation(
        scope, alice.id, "visited", org.id, valid_from=start, valid_to=start + timedelta(days=2)
    )
    exported = source.repository.export_json()
    source.close()

    target = build_bundle(database=tmp_path / "ri.db", fts=tmp_path / "ri.fts", vectors=tmp_path / "ri.vec")
    target.repository.import_json(exported)
    assert _m0_records(target.repository.export_json()) == _m0_records(exported)
    target.close()


# --- M0 fail-closed pins: the new acceptance paths must still reject forgeries ---


def _m0_exported_records(tmp_path, content="Alice works at Acme"):
    bundle = build_bundle(database=tmp_path / "f.db", fts=tmp_path / "f.fts", vectors=tmp_path / "f.vec")
    scope = _m0_scope("m0f", "m0fc")
    bundle.repository.create_collection(Collection(id="m0fc", tenant_id="m0f", name="M0 Forged"))
    bundle.service.ingest_observation(scope, content=content)
    DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    records = json.loads(bundle.repository.export_json())["records"]
    bundle.close()
    return records


def test_forged_relation_identity_is_rejected(tmp_path):
    records = _m0_exported_records(tmp_path)
    relation = next(r for r in records if r["record_type"] == "relation")
    relation["identity_key"] = "0" * 64  # matches neither writer scheme
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_snapshot(records)


def test_forged_task_note_key_on_retired_version_is_rejected(tmp_path):
    bundle = build_bundle(database=tmp_path / "tn.db", fts=tmp_path / "tn.fts", vectors=tmp_path / "tn.vec")
    scope = _m0_scope("m0tn", "m0tnc")
    bundle.repository.create_collection(Collection(id="m0tnc", tenant_id="m0tn", name="M0 TN"))
    bundle.service.set_task_note(scope, task="m0-forged", state="v1", metadata={})
    bundle.service.set_task_note(scope, task="m0-forged", state="v2", metadata={})
    records = json.loads(bundle.repository.export_json())["records"]
    bundle.close()
    notes = [r for r in records if r["record_type"] == "task_note"]
    current = next(r for r in notes if r["status"] == "current")
    retired = next(r for r in notes if r["status"] == "replaced")
    # A retired version carrying the current key could masquerade as current.
    retired["identity_key"] = current["identity_key"]
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_snapshot(records)


def test_forged_task_note_status_is_rejected(tmp_path):
    bundle = build_bundle(database=tmp_path / "ts.db", fts=tmp_path / "ts.fts", vectors=tmp_path / "ts.vec")
    scope = _m0_scope("m0ts", "m0tsc")
    bundle.repository.create_collection(Collection(id="m0tsc", tenant_id="m0ts", name="M0 TS"))
    bundle.service.set_task_note(scope, task="m0-status", state="v1", metadata={})
    records = json.loads(bundle.repository.export_json())["records"]
    bundle.close()
    note = next(r for r in records if r["record_type"] == "task_note")
    note["status"] = "bogus"
    note["identity_key"] = None  # isolate the status check from the identity check
    with pytest.raises(ValueError, match="invalid task_note status"):
        validate_snapshot(records)
