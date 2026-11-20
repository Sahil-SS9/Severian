"""M1 — a real purge: the erased text leaves every place the engine stores it.

Pinned end to end through the production composition (``build_bundle``) and the
production worker (``DerivationWorker.drain``), as the fleet runs them:

- erasing an observation, on its own, is still a tombstone: the text stays in the
  canonical row, the export and both histories (this test pins that so nobody can
  claim erasure is physical removal) — the purge is the operation that removes it;
- ``purge`` scrubs the whole observation revision chain, every memory derived from
  it (revisions and tombstones included), the relations that lose their last
  support and the entity names nothing else supports;
- after ``purge --confirm`` + compaction, no SQLite store file (canonical, FTS,
  vector, or their ``-wal``/``-shm`` siblings) holds the marker bytes;
- the store still exports and re-imports after a purge;
- re-stating the same fact after an erase or a purge is a NEW findable observation
  (M1 owner decision (a)) — it never reactivates the old row;
- a crash in the middle of an erasure can no longer leave a derived memory active
  under a source that is already gone (MG-3);
- the dry run lists ids and counts only, never content, and changes nothing.

All stores are synthetic (``tmp_path``). ``HOME``/``HERMES_HOME``/``SEVERIAN_STORAGE``
are redirected below, before any adapter import, so no call in this module can
resolve a home-default store.
"""

from __future__ import annotations

# ruff: noqa: E402 — the store guard below MUST run before any adapter is imported.
import hashlib
import json
import os
import sqlite3
import tempfile

# -- test safety guard (M1 §2.6): before any severian import --------------------
_GUARD = tempfile.mkdtemp(prefix="sev-m1-guard-")
os.environ["HOME"] = _GUARD
os.environ["HERMES_HOME"] = os.path.join(_GUARD, "hermes")
os.environ["SEVERIAN_STORAGE"] = os.path.join(_GUARD, "guard.db")

from pathlib import Path

import pytest

from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.errors import NotFoundError
from severian.domain.models import ERASED, Collection, Scope
from severian.infrastructure.sqlite import SQLiteRepository

# The fixed removal marker (M1 owner decision (c)). Written as a literal here so this
# module still collects on a tree where the constant does not exist yet, and pinned to
# the production constant by ``test_the_removal_marker_is_the_fixed_literal``.
PURGED_TEXT = "[purged]"

SCOPE = Scope(
    tenant_id="kensei",
    profile_id="octacon",
    collection_id="purge-profile",
    agent_id="octacon",
    session_id="shared",
)

OTHER_SCOPE = Scope(
    tenant_id="kensei",
    profile_id="octacon",
    collection_id="purge-profile",
    agent_id="octacon",
    session_id="other",
)

# A made-up marker. Never real memory content (ground rule 9).
MARKER = "ZQX-PURGE-MARKER-9F3A"
SECRET = f"The pantry code is {MARKER} for the north shelf"
OTHER_SECRET = f"The spare key is {MARKER} under the blue pot"


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    built.repository.create_collection(Collection(id=SCOPE.collection_id, tenant_id=SCOPE.tenant_id, name="P"))
    yield built
    built.close()


def drain(bundle: ServiceBundle) -> None:
    summary = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    assert summary.failed == 0 and summary.projection_failed == 0


def ingest(bundle: ServiceBundle, content: str) -> str:
    return bundle.service.ingest_observation(SCOPE, content=content, source={"session_id": "chat-1"}).observation_id


def store_files(bundle: ServiceBundle) -> list[Path]:
    """The three SQLite stores the bundle owns, plus WAL/SHM siblings when present."""
    paths: list[Path] = []
    for base in (Path(bundle.repository.path), Path(bundle.lexical.path), Path(bundle.vector.path)):
        for candidate in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
            if candidate.exists():
                paths.append(candidate)
    return paths


def store_paths(bundle: ServiceBundle) -> list[Path]:
    """The three durable store files (no sidecars): what a byte comparison must cover."""
    return [Path(bundle.repository.path), Path(bundle.lexical.path), Path(bundle.vector.path)]


def checkpoint(paths: list[Path]) -> None:
    """Flush and truncate each store's WAL, so a byte comparison sees an at-rest state.

    Without this a no-op read looks like a write: the pages may sit in the WAL until
    something else checkpoints them.
    """
    for path in paths:
        if not path.exists():
            continue
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()


def marker_hits(paths: list[Path]) -> list[str]:
    needle = MARKER.lower().encode()
    return [str(path) for path in paths if path.exists() and needle in path.read_bytes().lower()]


# -- the defect this item fixes is real: erasure alone leaves the text ----------


def test_erasure_alone_leaves_the_text_in_the_row_the_export_and_both_histories(
    bundle: ServiceBundle,
) -> None:
    """Characterisation of the base behaviour: erasing is not purging (MG-7 / threat model).

    Pinned so no operator can read ``erase`` as physical removal, and so the M1
    purge has a baseline it must beat.
    """
    original = ingest(bundle, SECRET)
    drain(bundle)
    revision = bundle.repository.revise_observation(SCOPE, original, f"{SECRET}, south shelf").id
    drain(bundle)
    bundle.service.erase_observation(SCOPE, revision)
    drain(bundle)

    rows = bundle.repository.list_observations_history(SCOPE)
    assert any(MARKER in row.content for row in rows)
    assert any(MARKER in row.raw_content for row in rows)
    assert MARKER in bundle.repository.export_json()
    assert any(MARKER in memory.content for memory in bundle.repository.list_memories_history(SCOPE))


# -- MG-2: re-stating after a forget is new evidence, not a resurrection -------


def test_restating_after_an_erasure_stores_a_new_findable_observation(bundle: ServiceBundle) -> None:
    """M1 owner decision (a): the erased row must not satisfy the content dedupe."""
    first = ingest(bundle, SECRET)
    drain(bundle)
    bundle.service.erase_observation(SCOPE, first)
    drain(bundle)
    assert bundle.service.search(SCOPE, query="pantry code north shelf", limit=10) == []

    second = ingest(bundle, SECRET)
    drain(bundle)

    assert second != first, "the re-statement was silently deduped onto the erased row"
    assert bundle.repository.get_observation(SCOPE, first).superseded_by_id is not None, "the old row was resurrected"
    hits = bundle.service.search(SCOPE, query="pantry code north shelf", limit=10)
    assert [hit.memory_id for hit in hits], "the re-stated fact was stored but derives nothing findable"


def test_restating_after_a_purge_stores_a_new_findable_observation(bundle: ServiceBundle) -> None:
    first = ingest(bundle, SECRET)
    drain(bundle)
    bundle.service.purge_observation(SCOPE, first)
    drain(bundle)

    second = ingest(bundle, SECRET)
    drain(bundle)

    assert second != first
    assert bundle.repository.get_observation(SCOPE, first).purged is True
    assert [hit.memory_id for hit in bundle.service.search(SCOPE, query="pantry code north shelf", limit=10)]


# -- the purge itself ----------------------------------------------------------


def test_the_removal_marker_is_the_fixed_literal() -> None:
    """M1 owner decision (c): one fixed marker, so the digests over it are fixed too."""
    from severian.domain.models import PURGE_MARKER

    assert PURGE_MARKER == PURGED_TEXT


def test_purge_scrubs_the_whole_observation_revision_chain(bundle: ServiceBundle) -> None:
    root = ingest(bundle, SECRET)
    drain(bundle)
    head = bundle.repository.revise_observation(SCOPE, root, f"{SECRET}, south shelf").id
    drain(bundle)
    bundle.service.erase_observation(SCOPE, head)

    report = bundle.service.purge_observation(SCOPE, root)

    assert root in report.plan.observation_ids
    assert head in report.plan.observation_ids
    assert len(report.plan.observation_ids) == 3, "root + revision + erasure tombstone"
    marker_hash = hashlib.sha256(PURGED_TEXT.encode()).hexdigest()
    for observation in bundle.repository.list_observations_history(SCOPE):
        assert observation.content == PURGED_TEXT
        assert observation.raw_content == PURGED_TEXT
        assert observation.content_hash == marker_hash
        assert observation.raw_hash == marker_hash
        assert observation.purged is True
        assert observation.source == {}
        assert observation.attachments == []
        assert observation.superseded_by_id is not None  # never an active head again


def test_purge_scrubs_every_derived_memory_including_revisions(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)
    [memory] = bundle.repository.list_memories(SCOPE)
    bundle.service.correct_memory(SCOPE, memory.id, content=f"{SECRET} (corrected)", model_id="m", prompt_version="v")

    bundle.service.purge_observation(SCOPE, observation)

    history = bundle.repository.list_memories_history(SCOPE)
    assert len(history) >= 2
    marker_hash = hashlib.sha256(PURGED_TEXT.encode()).hexdigest()
    for stored in history:
        assert stored.content == PURGED_TEXT
        assert stored.purged is True
        assert stored.cues == []
        assert stored.trigger is None
        assert stored.superseded_by_id is not None
    assert bundle.service.search(SCOPE, query="pantry code north shelf", limit=10) == []
    assert MARKER not in bundle.repository.export_json()
    assert hashlib.sha256(PURGED_TEXT.encode()).hexdigest() == marker_hash


def test_purge_leaves_no_marker_bytes_in_any_sqlite_store(bundle: ServiceBundle) -> None:
    """The acceptance scan: canonical, FTS and vector files after checkpoint + VACUUM."""
    observation = ingest(bundle, SECRET)
    drain(bundle)
    assert marker_hits(store_files(bundle)), "precondition: the marker is on disk before the purge"

    bundle.service.purge_observation(SCOPE, observation)
    bundle.compact()
    bundle.close()
    # Scan the files at rest, after the bundle's connections are closed: that is the state
    # an operator's `purge --confirm` leaves behind.
    paths = store_files(bundle)

    assert paths, "the test must scan real store files"
    assert marker_hits(paths) == []


def test_purge_retracts_relations_that_lose_their_last_support(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)
    bundle.repository.publish_derivation(
        SCOPE,
        entities=[("person", "Alice"), ("org", "Acme")],
        relations=[("Alice", "works_at", "Acme")],
        memories=[],
        source_observation_id=observation,
    )
    [relation] = [r for r in bundle.repository.list_relations(SCOPE) if r.source_observation_ids]

    report = bundle.service.purge_observation(SCOPE, observation)

    assert report.plan.relation_ids == (relation.id,)
    stored = bundle.repository.get_relation(SCOPE, relation.id)
    assert stored.source_observation_ids == []
    assert stored.retracted_at is not None, "a relation with no support left must leave the current graph"


def test_purge_keeps_a_relation_another_source_still_supports(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)
    bundle.repository.publish_derivation(
        SCOPE,
        entities=[("person", "Alice"), ("org", "Acme")],
        relations=[("Alice", "works_at", "Acme")],
        memories=[],
        source_observation_id=observation,
    )
    other = ingest(bundle, "Bob works at Acme")
    drain(bundle)
    bundle.repository.publish_derivation(
        SCOPE,
        entities=[("person", "Bob"), ("org", "Acme")],
        relations=[("Bob", "works_at", "Acme")],
        memories=[],
        source_observation_id=other,
    )

    bundle.service.purge_observation(SCOPE, observation)

    live = [relation for relation in bundle.repository.list_relations(SCOPE) if relation.retracted_at is None]
    assert live, "the relation another source supports must stay in the graph"
    assert all(observation not in relation.source_observation_ids for relation in live), (
        "no live relation may still cite the purged observation"
    )
    assert any(other in relation.source_observation_ids for relation in live)
    entities = bundle.repository.list_entities(SCOPE)
    assert all(entity.purged is False for entity in entities if entity.name.casefold() == "acme"), (
        "an entity another source still supports keeps its name"
    )
    assert any(entity.purged is True for entity in entities), "the unsupported entity was scrubbed"


def test_purge_scrubs_an_entity_name_only_when_nothing_else_supports_it(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)
    bundle.repository.publish_derivation(
        SCOPE,
        entities=[("person", "Alice"), ("org", "Acme")],
        relations=[("Alice", "works_at", "Acme")],
        memories=[],
        source_observation_id=observation,
    )

    report = bundle.service.purge_observation(SCOPE, observation)

    assert set(report.plan.entity_ids) == {entity.id for entity in bundle.repository.list_entities(SCOPE)}
    for entity in bundle.repository.list_entities(SCOPE):
        assert entity.name == PURGED_TEXT
        assert entity.purged is True


def test_purge_writes_audit_rows_with_ids_and_counts_and_no_content(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)

    bundle.service.purge_observation(SCOPE, observation)

    rows = bundle.repository.list_audit(SCOPE)
    actions = [row.action for row in rows]
    assert "observation.purge" in actions
    assert "memory.purge" in actions
    for row in rows:
        assert row.target_id
        assert MARKER not in json.dumps(row.metadata)
        assert "pantry" not in json.dumps(row.metadata).lower()


def test_purge_cancels_pending_derivation_so_nothing_is_rebuilt_from_the_marker(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)  # not drained: its derive job is still pending

    bundle.service.purge_observation(SCOPE, observation)
    drain(bundle)

    assert bundle.repository.list_memories(SCOPE) == []
    assert MARKER not in bundle.repository.export_json()


def test_the_purged_store_still_exports_and_reimports(bundle: ServiceBundle, tmp_path: Path) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)
    bundle.service.purge_observation(SCOPE, observation)

    document = json.loads(bundle.repository.export_json())
    document["checksum"] = bundle.repository.export_checksum(document["records"])
    target = SQLiteRepository(tmp_path / "m1-reimport.db")
    target.initialize()
    try:
        target.import_json(json.dumps(document, sort_keys=True))
        assert [row.purged for row in target.list_observations_history(SCOPE)] == [True]
        assert MARKER not in target.export_json()
    finally:
        target.close()


def test_the_dry_run_changes_nothing_and_never_prints_content(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)
    checkpoint(store_paths(bundle))
    before = {path: path.read_bytes() for path in store_paths(bundle)}

    plan = bundle.service.preview_purge(SCOPE, observation)

    checkpoint(store_paths(bundle))
    after = {path: path.read_bytes() for path in store_paths(bundle)}
    assert before == after, "the dry run must not write a byte"
    assert observation in plan.observation_ids
    assert plan.counts["memory"] == 1
    dumped = json.dumps(plan.to_dict())
    assert MARKER not in dumped and "pantry" not in dumped.lower()


def test_purge_is_scope_bound_and_idempotent(bundle: ServiceBundle) -> None:
    observation = ingest(bundle, SECRET)
    drain(bundle)

    with pytest.raises(NotFoundError):
        bundle.service.purge_observation(OTHER_SCOPE, observation)

    first = bundle.service.purge_observation(SCOPE, observation)
    audits_after_first = len(bundle.repository.list_audit(SCOPE))
    second = bundle.service.purge_observation(SCOPE, observation)

    assert first.plan == second.plan
    assert second.memories == 0, "a repeated purge changes nothing, so it scrubs nothing new"
    assert len(bundle.repository.list_audit(SCOPE)) == audits_after_first, "a no-op purge audits nothing"


def test_a_crash_in_the_middle_of_erasing_leaves_no_orphan_derived_memory(
    bundle: ServiceBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MG-3: the erasure cascade is ONE transaction, so a failure cannot half-apply it."""
    observation = ingest(bundle, SECRET)
    drain(bundle)
    [memory] = bundle.repository.list_memories(SCOPE)
    original = SQLiteRepository._insert_audit_in_transaction

    def boom(self, connection, scope, action, target_id, metadata):  # type: ignore[no-untyped-def]
        if action == "memory.supersede":
            raise RuntimeError("injected crash before the tombstone link committed")
        return original(connection, scope, action, target_id, metadata)

    monkeypatch.setattr(SQLiteRepository, "_insert_audit_in_transaction", boom)
    with pytest.raises(RuntimeError, match="injected crash"):
        bundle.service.erase_observation(SCOPE, observation)
    monkeypatch.undo()

    stored_observation = bundle.repository.get_observation(SCOPE, observation)
    stored_memory = bundle.repository.get_memory(SCOPE, memory.id)
    assert not (stored_observation.superseded_by_id is not None and stored_memory.superseded_by_id is None), (
        "the erasure of the source committed while its derived memory stayed active"
    )
    assert [row.id for row in bundle.repository.list_memories_history(SCOPE)] == [memory.id], (
        "a half-written tombstone was left behind"
    )


def test_erase_observation_still_reads_as_before_when_it_is_not_purged(bundle: ServiceBundle) -> None:
    """Erasures that are not purged keep the exact tombstone shape the product shipped."""
    observation = ingest(bundle, SECRET)
    drain(bundle)
    [memory] = bundle.repository.list_memories(SCOPE)

    bundle.service.erase_observation(SCOPE, observation)

    stored = bundle.repository.get_observation(SCOPE, observation)
    tombstone = bundle.repository.get_observation(SCOPE, stored.superseded_by_id or "")
    assert tombstone.content == "[erased]"
    assert tombstone.superseded_by_id == ERASED
    erased_memory = bundle.repository.get_memory(SCOPE, memory.id)
    assert erased_memory.superseded_by_id not in (None, ERASED)
    assert bundle.repository.get_memory(SCOPE, erased_memory.superseded_by_id or "").superseded_by_id == ERASED


# -- F1 (M1 correction): erase first, then purge, must still scrub -------------


def _publish_graph(bundle: ServiceBundle, observation: str) -> None:
    bundle.repository.publish_derivation(
        SCOPE,
        entities=[("person", "Zed Quanley"), ("org", "Quanco")],
        relations=[("Zed Quanley", "works_at", "Quanco")],
        memories=[],
        source_observation_id=observation,
    )


def _names_in_stores(bundle: ServiceBundle, *names: str) -> list[str]:
    """Store files whose raw bytes still hold any of ``names`` (casefolded scan)."""
    needles = [name.casefold().encode() for name in names]
    hits: list[str] = []
    for path in store_files(bundle):
        data = path.read_bytes().lower()
        if any(needle in data for needle in needles):
            hits.append(str(path))
    return hits


def test_erasing_first_then_purging_still_scrubs_the_relations_and_entity_names(
    bundle: ServiceBundle,
) -> None:
    """F1: the operator's most likely order is erase, then purge.

    Erasing retracts the relation that cited the observation and drops the citation from
    its active support. The purge must still reach that relation and the entity names it
    supported, exactly as a direct purge does — otherwise the observation text is gone
    while the names derived from it survive in the canonical rows, the export and the
    store files. The withdrawn support must not be counted as active support again: the
    relation stays retracted, and the plan reaches it through its recorded history.
    """
    observation = ingest(bundle, SECRET)
    drain(bundle)
    _publish_graph(bundle, observation)

    bundle.service.erase_observation(SCOPE, observation)
    [retracted] = [
        row for row in bundle.repository.list_relations(SCOPE) if row.source_observation_ids or row.retracted_at
    ]
    assert retracted.retracted_at is not None
    assert retracted.source_observation_ids == [], "an erased source is no longer active support"

    plan = bundle.service.preview_purge(SCOPE, observation)
    assert plan.relation_ids == (retracted.id,), "the purge could not reach the relation the erase retracted"
    assert len(plan.entity_ids) == 2, "the purge could not reach the entity names the erased relation supported"

    report = bundle.service.purge_observation(SCOPE, observation)

    assert report.relations == 1 and report.entities == 2
    assert sorted(entity.name for entity in bundle.repository.list_entities(SCOPE)) == [PURGED_TEXT, PURGED_TEXT]
    exported = bundle.repository.export_json()
    assert "Zed Quanley" not in exported and "Quanco" not in exported
    stored = bundle.repository.get_relation(SCOPE, retracted.id)
    assert stored.source_observation_ids == [] and stored.retracted_at is not None

    actions = [row.action for row in bundle.repository.list_audit(SCOPE)]
    assert actions.count("entity.purge") == 2, "a scrub without an audit row is a silent deletion"
    assert actions.count("relation.purge") == 1

    # The names must also be gone from the files themselves, not just from the payloads.
    bundle.compact()
    assert _names_in_stores(bundle, "Zed Quanley", "Quanco") == [], (
        "entity names survived in a store file after compaction"
    )


def test_purging_an_unrelated_chain_leaves_a_relation_retracted_for_another_source(
    bundle: ServiceBundle,
) -> None:
    """F1, conservative direction: recorded provenance must not widen a purge to the graph.

    A relation retracted because a DIFFERENT source was erased is untouched by a purge of
    this chain. Only the chain that actually supported it may reach it.
    """
    first = ingest(bundle, SECRET)
    drain(bundle)
    _publish_graph(bundle, first)
    [relation] = [row for row in bundle.repository.list_relations(SCOPE) if row.retracted_at is None]

    second = ingest(bundle, OTHER_SECRET)
    drain(bundle)
    bundle.service.erase_observation(SCOPE, first)
    entity_ids = {entity.id for entity in bundle.repository.list_entities(SCOPE)}

    plan = bundle.service.preview_purge(SCOPE, second)

    assert relation.id not in plan.relation_ids, "an unrelated chain reached this relation"
    assert not entity_ids.intersection(plan.entity_ids)
    bundle.service.purge_observation(SCOPE, second)
    assert sorted(entity.name for entity in bundle.repository.list_entities(SCOPE)) == ["Quanco", "Zed Quanley"]

    # ...and the chain that DID support it still reaches it.
    after = bundle.service.preview_purge(SCOPE, first)
    assert after.relation_ids == (relation.id,)
    assert set(after.entity_ids) == entity_ids


# -- F2 (M1 correction): a legacy erased row must not block a restatement ------


def test_a_legacy_erased_row_does_not_block_restating_the_same_text(tmp_path: Path) -> None:
    """F2: stores written before M1 hold erased rows that still carry their identity key.

    The active-only dedupe correctly finds no current evidence for the same text, so the
    ingest inserts — and used to collide on the unique index and raise out of the service.
    A restated fact must be stored as new evidence; the legacy row is otherwise untouched.
    """
    database, fts, vectors = tmp_path / "legacy.db", tmp_path / "legacy.fts", tmp_path / "legacy.vec"
    scoped = SCOPE.model_copy(update={"collection_id": "legacy-profile"})
    bundle = build_bundle(database=database, fts=fts, vectors=vectors)
    bundle.repository.create_collection(
        Collection(id=scoped.collection_id, tenant_id=scoped.tenant_id, name="L")
    )
    try:
        first = bundle.service.ingest_observation(scoped, content=SECRET, source={}).observation_id
    finally:
        bundle.close()

    # The pre-M1 erasure shape: tombstoned payload, identity key left in place.
    connection = sqlite3.connect(database, timeout=30, isolation_level=None)
    try:
        row = connection.execute(
            "SELECT payload FROM records WHERE id = ? AND record_type = 'observation'",
            (first,),
        ).fetchone()
        legacy_payload = json.loads(row[0])
        legacy_payload["superseded_by_id"] = ERASED
        connection.execute(
            "UPDATE records SET payload = ? WHERE id = ? AND record_type = 'observation'",
            (json.dumps(legacy_payload), first),
        )
        kept_key = connection.execute("SELECT identity_key FROM records WHERE id = ?", (first,)).fetchone()[0]
    finally:
        connection.close()
    assert kept_key is not None, "precondition: a legacy erased row keeps its identity key"

    bundle = build_bundle(database=database, fts=fts, vectors=vectors)
    try:
        second = bundle.service.ingest_observation(scoped, content=SECRET, source={}).observation_id
        assert second != first, "the restatement was not stored as new evidence"
        stored = bundle.repository.get_observation(scoped, first)
        assert stored.superseded_by_id == ERASED, "the legacy row was rewritten"
        assert stored.content == SECRET, "the legacy row lost its historical text"
        active = [row.id for row in bundle.repository.list_observations(scoped) if row.superseded_by_id is None]
        assert active == [second], "the legacy row was resurrected or the new row is not the only active head"

        connection = sqlite3.connect(database, timeout=30, isolation_level=None)
        try:
            released = connection.execute("SELECT identity_key FROM records WHERE id = ?", (first,)).fetchone()[0]
        finally:
            connection.close()
        assert released is None, "the blocking identity key was not released"
    finally:
        bundle.close()


# -- F6 (M1 correction): task notes are out of scope, pinned -------------------


def test_a_task_note_holding_the_target_text_is_out_of_scope_and_survives_a_purge(
    bundle: ServiceBundle,
) -> None:
    """F6: notes are working state, not evidence, and no purge may reach them.

    This is deliberate out-of-scope data, so it is NOT evidence for an "every file is
    clean" claim: the raw-byte purge tests use stores that never held a task-note copy of
    the target text. What this test pins is the scope boundary itself - the note rows are
    byte-identical before and after a purge of the observation holding the same text.
    """
    observation = ingest(bundle, SECRET)
    drain(bundle)
    bundle.service.set_task_note(SCOPE, "pantry-audit", f"check the {MARKER} shelf label")
    bundle.service.set_task_note(SCOPE, "pantry-audit", f"{MARKER} verified, next: reorder")
    current, history = bundle.service.get_task_note(SCOPE, "pantry-audit")
    assert current is not None and history, "precondition: a current note and its history"
    before = [note.model_dump(mode="json") for note in [current, *history]]

    bundle.service.purge_observation(SCOPE, observation)

    current_after, history_after = bundle.service.get_task_note(SCOPE, "pantry-audit")
    assert current_after is not None
    after = [note.model_dump(mode="json") for note in [current_after, *history_after]]
    assert after == before, "the purge reached a task note"
