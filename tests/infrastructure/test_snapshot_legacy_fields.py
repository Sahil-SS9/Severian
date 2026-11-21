"""0.6.1 — a snapshot of a store that holds rows written before newer model fields existed.

Memories and relations written by older releases (Aug-Oct 2026) are stored without the keys
later models added with a default: memories lack ``occurred_at``, ``occurred_until``,
``cues``, ``trigger``, ``valid_until``, ``replaced_by_id`` (and ``purged``); relations lack
``source_observation_ids``, ``retracted_at`` (and ``retracted_for_ids``). The live read path
fills those defaults, so such a store works, exports and searches - but ``import_json``
rejected every such row as "does not round-trip", so the store's own snapshot could not be
restored.

The tolerance is exactly those keys, on exactly those kinds: anything else missing, and every
hash / identity / scope / reference check, still fails closed. Imported rows are stored as
given (no rewrite), so a restored store exports byte-identical to its source.

All memory text here is made up.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from severian.composition import build_bundle
from severian.domain.models import Collection, Entity, Memory, Observation, Relation, Scope
from severian.infrastructure.snapshot import identity_hash, validate_snapshot

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")
SCOPE = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
SCOPE_DIMENSIONS = ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")

# The exact key sets measured on the live store's failing rows (21/11/2026).
OLD_MEMORY_MISSING = ("cues", "occurred_at", "occurred_until", "purged", "replaced_by_id", "trigger", "valid_until")
OLD_RELATION_MISSING = ("retracted_at", "retracted_for_ids", "source_observation_ids")
QUERIES = ("Alice works at", "Acme", "deploy window", "Globex", "Tuesday")


def _item(record_type: str, model: Any, *, identity_key: str | None, content_hash: str | None = None) -> dict[str, Any]:
    return {
        "id": model.id,
        "record_type": record_type,
        **{dimension: getattr(model.scope, dimension) for dimension in SCOPE_DIMENSIONS},
        "content_hash": content_hash,
        "identity_key": identity_key,
        "status": None,
        "payload": model.model_dump(mode="json"),
    }


def _old_graph() -> list[dict[str, Any]]:
    """Observation + memory + two entities + relation, with the memory and relation in the old shape."""
    text = "Alice works at Acme"
    digest = hashlib.sha256(text.encode()).hexdigest()
    observation = Observation(scope=SCOPE, content=text, content_hash=digest, raw_content=text, raw_hash=digest)
    memory = Memory(
        scope=SCOPE,
        kind="fact",
        content="Alice works at Acme",
        source_observation_ids=[observation.id],
        model_id="rules",
        prompt_version="v1",
        confidence=0.9,
    )
    alice = Entity(scope=SCOPE, kind="person", name="Alice")
    acme = Entity(scope=SCOPE, kind="org", name="Acme")
    relation = Relation(scope=SCOPE, subject_id=alice.id, predicate="works_at", object_id=acme.id)
    items = [
        _item(
            "observation",
            observation,
            content_hash=observation.content_hash,
            identity_key=identity_hash(observation.content_hash or ""),
        ),
        _item(
            "memory",
            memory,
            identity_key=identity_hash("fact", memory.content, [observation.id], "rules", "v1"),
        ),
        _item("entity", alice, identity_key=identity_hash("person", "alice")),
        _item("entity", acme, identity_key=identity_hash("org", "acme")),
        _item("relation", relation, identity_key=identity_hash("relation", alice.id, "works_at", acme.id)),
    ]
    for item in items:
        _make_old(item)
    return items


def _make_old(item: dict[str, Any]) -> None:
    missing = {"memory": OLD_MEMORY_MISSING, "relation": OLD_RELATION_MISSING}.get(item["record_type"], ())
    for key in missing:
        item["payload"].pop(key)


def _of(items: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(item for item in items if item["record_type"] == kind)


# -- the validator accepts the old shape ---------------------------------------


def test_an_old_memory_and_relation_validate() -> None:
    validate_snapshot(_old_graph())


# -- and nothing else is relaxed ------------------------------------------------


def test_a_memory_missing_a_key_outside_the_old_set_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "memory")["payload"].pop("usage_count")  # has a default, but every old row carries it
    with pytest.raises(ValueError, match="round-trip"):
        validate_snapshot(items)


def test_a_relation_missing_a_key_outside_the_old_set_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "relation")["payload"].pop("valid_from")
    with pytest.raises(ValueError, match="round-trip"):
        validate_snapshot(items)


def test_an_unknown_extra_key_on_an_old_memory_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "memory")["payload"]["smuggled"] = "x"
    with pytest.raises(ValueError, match="round-trip"):
        validate_snapshot(items)


def test_an_old_memory_whose_text_was_edited_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "memory")["payload"]["content"] = "Alice works at Globex"
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_snapshot(items)


def test_an_old_memory_with_a_null_key_while_active_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "memory")["identity_key"] = None
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_snapshot(items)


def test_an_old_relation_with_a_forged_key_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "relation")["identity_key"] = identity_hash("relation", "x", "works_at", "y")
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_snapshot(items)


def test_an_old_memory_moved_to_another_scope_by_column_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "memory")["tenant_id"] = "t2"
    with pytest.raises(ValueError, match="scope mismatch"):
        validate_snapshot(items)


def test_an_old_relation_with_an_endpoint_that_is_not_an_entity_is_still_rejected() -> None:
    items = _old_graph()
    _of(items, "relation")["payload"]["object_id"] = _of(items, "observation")["id"]
    relation = _of(items, "relation")["payload"]
    _of(items, "relation")["identity_key"] = identity_hash(
        "relation", relation["subject_id"], "works_at", relation["object_id"]
    )
    with pytest.raises(ValueError, match="non-entity"):
        validate_snapshot(items)


def test_the_validator_does_not_rewrite_the_records_it_checks() -> None:
    items = _old_graph()
    before = json.dumps(items, sort_keys=True)
    validate_snapshot(items)
    assert json.dumps(items, sort_keys=True) == before


# -- store level, both backends ------------------------------------------------


def _ensure_pg_database(admin_dsn: str, name: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
        if exists is None:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def _build(backend: str, tmp_path: Path, tag: str, *, target: bool = False) -> Any:
    if backend == "sqlite":
        return build_bundle(
            database=tmp_path / f"{tag}.db", fts=tmp_path / f"{tag}.fts", vectors=tmp_path / f"{tag}.vec"
        )
    if target:
        _ensure_pg_database(PG_DSN, "severian_dst")
        return build_bundle(backend="postgresql", dsn=PG_DSN.rsplit("/", 1)[0] + "/severian_dst")
    return build_bundle(backend="postgresql", dsn=PG_DSN)


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_a_store_snapshot_holding_old_rows_imports_and_searches_the_same(tmp_path: Path, backend: str) -> None:
    if backend == "postgresql" and not PG_DSN:
        pytest.skip("SEVERIAN_PG_DSN not set; live PostgreSQL lane")
    from severian.application.worker import DerivationWorker

    unique = uuid.uuid4().hex[:10]
    tenant, collection = f"old{unique}", f"oldc{unique}"
    scope = Scope(tenant_id=tenant, profile_id="p1", collection_id=collection, agent_id="a1", session_id="s1")
    source = _build(backend, tmp_path, "src")
    source.repository.create_collection(Collection(id=collection, tenant_id=tenant, name="Old"))
    worker = DerivationWorker(source.repository, source.derivation, source.projection)
    for text in ("Alice works at Acme", "Alice works at Globex", "The deploy window is Tuesday"):
        source.service.ingest_observation(scope, content=text)
        worker.drain(gateway=source.gateway)
    alice = source.repository.add_entity(scope, "person", "Old Shape Alice")
    org = source.repository.add_entity(scope, "org", "Old Shape Org")
    source.repository.add_relation(scope, alice.id, "visited", org.id)
    before = [[hit.memory_id for hit in source.service.search(scope, query=q, limit=10)] for q in QUERIES]

    document = json.loads(source.repository.export_json())
    document["records"] = [r for r in document["records"] if r["tenant_id"] == tenant]
    for record in document["records"]:
        _make_old(record)
    kinds = [r["record_type"] for r in document["records"]]
    assert kinds.count("memory") >= 1 and kinds.count("relation") >= 1
    document["checksum"] = source.repository.export_checksum(document["records"])
    exported = json.dumps(document, sort_keys=True)
    source.close()

    target = _build(backend, tmp_path, "dst", target=True)
    target.repository.import_json(exported)
    target.repository.import_json(exported)  # idempotent: the stored rows equal the incoming ones
    target.projection.rebuild(scope)
    after = [[hit.memory_id for hit in target.service.search(scope, query=q, limit=10)] for q in QUERIES]
    assert after == before
    restored = {r["id"]: r for r in json.loads(target.repository.export_json())["records"] if r["tenant_id"] == tenant}
    assert restored == {r["id"]: r for r in document["records"]}  # stored as given, not rewritten
    memories = target.repository.list_memories(scope)
    assert memories and all(m.cues == [] and m.occurred_at is None and m.replaced_by_id is None for m in memories)
    target.close()
