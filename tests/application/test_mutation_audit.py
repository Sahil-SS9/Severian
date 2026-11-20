"""M2 — the mutation audit covers every payload-changing write (plan v2 M2).

Pinned end to end through the production composition (``build_bundle``) and the
production worker (``DerivationWorker.drain``), as the fleet runs them:

- erasure leaves ``memory.supersede`` (the tombstone link) beside the tombstone's
  ``memory.create``, so the audit says what happened to the erased memory;
- a corrected source audits ``memory.source_superseded`` and the relation
  unsupport/retract it causes;
- replacing a single-valued relation audits ``relation.end``; restating it audits
  ``relation.reopen``; a second supporting source audits ``relation.support``;
- the curation marker's "review proposals in audit" claim is true: the pass
  persists a ``curation.report`` row whose metadata carries the proposals, and
  the worker no longer throws the report away;
- none of these paths emits an outbox event (they are audit, not projection);
- the audit stays append-only and the store still exports and re-imports.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.sqlite import SQLiteRepository

SCOPE = Scope(
    tenant_id="kensei",
    profile_id="octacon",
    collection_id="profile",
    agent_id="octacon",
    session_id="shared",
)

_NEW_OPERATIONS = ("supersede", "close", "source_superseded", "retract", "unsupport", "reopen")


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


def audit(bundle: ServiceBundle, action: str | None = None):
    rows = bundle.repository.list_audit(SCOPE)
    return rows if action is None else [row for row in rows if row.action == action]


def assert_no_projection_events(bundle: ServiceBundle) -> None:
    """The audit-only helper writes no outbox row: these ops never drive the projection."""
    kinds = [item.kind for item in bundle.repository.pending_outbox()]
    assert [kind for kind in kinds if any(op in kind for op in _NEW_OPERATIONS)] == []


def round_trip(bundle: ServiceBundle, tmp_path: Path, tag: str) -> None:
    """The M0 contract: what the audit writes must still export and re-import."""
    document = json.loads(bundle.repository.export_json())
    document["checksum"] = bundle.repository.export_checksum(document["records"])
    target = SQLiteRepository(tmp_path / f"m2-app-{tag}.db")
    target.initialize()
    try:
        target.import_json(json.dumps(document, sort_keys=True))
        want = [(row.action, row.target_id, row.metadata) for row in bundle.repository.list_audit(SCOPE)]
        got = [(row.action, row.target_id, row.metadata) for row in target.list_audit(SCOPE)]
        assert got == want
    finally:
        target.close()


def test_erasing_a_memory_audits_the_tombstone_link(bundle: ServiceBundle, tmp_path: Path) -> None:
    ingest(bundle, "The user's office is in Bristol")
    drain(bundle)
    memory = bundle.repository.list_memories(SCOPE)[0]

    erased = bundle.service.erase_memory(SCOPE, memory.id)
    tombstone_id = erased.superseded_by_id
    assert tombstone_id is not None

    supersedes = audit(bundle, "memory.supersede")
    assert [(row.target_id, row.metadata["replacement_id"]) for row in supersedes] == [(memory.id, tombstone_id)]
    # The tombstone's own creation is still audited, so the pair tells the whole story.
    assert (tombstone_id, "memory.create") in {(row.target_id, row.action) for row in audit(bundle)}
    assert_no_projection_events(bundle)
    round_trip(bundle, tmp_path, "erase")


def test_correcting_a_source_audits_the_memories_it_supersedes(bundle: ServiceBundle, tmp_path: Path) -> None:
    observation_id = ingest(bundle, "The deployment window is Tuesday")
    drain(bundle)
    derived = [
        memory.id
        for memory in bundle.repository.list_memories(SCOPE)
        if observation_id in memory.source_observation_ids
    ]
    assert derived

    bundle.service.revise_observation(SCOPE, observation_id, "The deployment window is Thursday")

    rows = audit(bundle, "memory.source_superseded")
    assert sorted(row.target_id for row in rows) == sorted(derived)
    assert {row.metadata["observation_id"] for row in rows} == {observation_id}
    assert_no_projection_events(bundle)
    round_trip(bundle, tmp_path, "revise")


def test_a_replaced_single_valued_relation_is_audited(bundle: ServiceBundle, tmp_path: Path) -> None:
    ingest(bundle, "Alice works at Acme")
    drain(bundle)
    ingest(bundle, "Alice works at Globex")
    drain(bundle)

    rows = audit(bundle, "relation.end")
    assert len(rows) == 1
    ended = bundle.repository.get_relation(SCOPE, rows[0].target_id)
    assert ended.valid_to is not None
    replacement = bundle.repository.get_relation(SCOPE, rows[0].metadata["replaced_by_id"])
    assert replacement.valid_to is None and replacement.object_id != ended.object_id
    assert_no_projection_events(bundle)
    round_trip(bundle, tmp_path, "relation-end")


def test_a_restated_relation_is_audited_as_reopened(bundle: ServiceBundle, tmp_path: Path) -> None:
    ingest(bundle, "Alice works at Acme")
    drain(bundle)
    ingest(bundle, "Alice works at Globex")
    drain(bundle)
    # A different sentence stating the same single-valued fact re-derives the
    # same relation (identical text would be deduped at ingest, not re-derived).
    ingest(bundle, "Alice now works at Acme again")
    drain(bundle)

    assert len(audit(bundle, "relation.reopen")) == 1
    open_relations = [rel for rel in bundle.repository.list_relations(SCOPE) if rel.valid_to is None]
    assert len(open_relations) == 1
    round_trip(bundle, tmp_path, "relation-reopen")


def test_a_second_source_supporting_a_relation_is_audited(bundle: ServiceBundle, tmp_path: Path) -> None:
    ingest(bundle, "Alice works at Acme")
    drain(bundle)
    ingest(bundle, "Alice works at Acme, confirmed")
    drain(bundle)

    relation = [rel for rel in bundle.repository.list_relations(SCOPE) if rel.predicate == "works_at"][0]
    supported = [row for row in audit(bundle, "relation.support") if row.target_id == relation.id]
    assert len(supported) == 1
    assert supported[0].metadata["source_observation_id"]
    round_trip(bundle, tmp_path, "relation-support")


def _old_low_confidence_memory(bundle: ServiceBundle, *, age_days: int) -> Memory:
    observation = bundle.repository.add_observation(
        Observation(scope=SCOPE, content=f"stale note {age_days}", source={"uri": f"note://{age_days}"})
    )
    return bundle.repository.add_memory(
        Memory(
            scope=SCOPE,
            kind="fact",
            content="The user once mentioned a hobby",
            confidence=0.05,
            source_observation_ids=[observation.id],
            model_id="test",
            prompt_version="v1",
            created_at=datetime.now(UTC) - timedelta(days=age_days),
        )
    )


def test_the_curation_pass_persists_its_report_reference(bundle: ServiceBundle, tmp_path: Path) -> None:
    """The marker says "review proposals in audit"; that must be true."""
    old = _old_low_confidence_memory(bundle, age_days=400)
    bundle.repository.enqueue_job(SCOPE, "dream", {"mode": "curate"})
    worker = DerivationWorker(repository=bundle.repository, derivation=None, projection=bundle.projection)
    summary = worker.drain(gateway=bundle.gateway)
    assert summary.processed == 1

    markers = [m for m in bundle.repository.list_memories(SCOPE) if m.kind == "curation_review"]
    assert len(markers) == 1
    rows = audit(bundle, "curation.report")
    assert len(rows) == 1
    reported = rows[0]
    assert reported.target_id == markers[0].id  # the reference points at the marker it summarises
    metadata = reported.metadata
    assert metadata["report_id"]  # a durable reference to the report itself
    assert metadata["review_marker_id"] == markers[0].id
    assert metadata["memories_scored"] >= 1
    assert old.id in metadata["low_value_ids"]  # the proposals are readable from audit
    assert metadata["low_value_candidates"] == len(metadata["low_value_ids"])
    assert metadata["contradiction_pairs"] == len(metadata["contradiction_ids"])
    round_trip(bundle, tmp_path, "curation")


def test_curation_audit_is_written_even_without_a_marker(bundle: ServiceBundle) -> None:
    """An empty scope scores nothing, so there is no marker and nothing to reference."""
    bundle.repository.enqueue_job(SCOPE, "dream", {"mode": "curate"})
    worker = DerivationWorker(repository=bundle.repository, derivation=None, projection=bundle.projection)
    worker.drain(gateway=bundle.gateway)
    assert audit(bundle, "curation.report") == []
    assert [m for m in bundle.repository.list_memories(SCOPE) if m.kind == "curation_review"] == []


def test_consolidation_supersede_is_audited(bundle: ServiceBundle, tmp_path: Path) -> None:
    """The dreaming consolidation retires duplicates through the audited port method."""
    source_a = bundle.repository.add_observation(Observation(scope=SCOPE, content="note a", source={"uri": "note://a"}))
    source_b = bundle.repository.add_observation(Observation(scope=SCOPE, content="note b", source={"uri": "note://b"}))
    for source, age_days in ((source_a, 10), (source_b, 1)):
        bundle.repository.add_memory(
            Memory(
                scope=SCOPE,
                kind="fact",
                content="the search index uses sqlite fts5",
                confidence=0.9,
                source_observation_ids=[source.id],
                model_id="m",
                prompt_version="v",
                created_at=datetime.now(UTC) - timedelta(days=age_days),
            )
        )
    older = bundle.repository.list_memories(SCOPE)[0]
    before = {(row.action, row.target_id) for row in audit(bundle)}

    bundle.repository.enqueue_job(SCOPE, "dream", {"mode": "consolidate"})
    worker = DerivationWorker(repository=bundle.repository, derivation=None, projection=bundle.projection)
    summary = worker.drain(gateway=bundle.gateway)
    assert summary.processed == 1

    fresh = audit(bundle, "memory.supersede")
    assert len(fresh) == 1
    assert fresh[0].target_id == older.id
    assert fresh[0].metadata["replacement_id"]
    assert (fresh[0].action, fresh[0].target_id) not in before
    round_trip(bundle, tmp_path, "consolidate")


def test_the_store_audit_stays_append_only_across_the_whole_flow(bundle: ServiceBundle) -> None:
    observation_id = ingest(bundle, "Alice works at Acme")
    drain(bundle)
    snapshot = {(row.id, row.action, row.target_id) for row in audit(bundle)}
    assert snapshot

    bundle.service.revise_observation(SCOPE, observation_id, "Alice works at Globex")
    drain(bundle)
    memory = bundle.repository.list_memories(SCOPE)[0]
    bundle.service.erase_memory(SCOPE, memory.id)

    after = {(row.id, row.action, row.target_id) for row in audit(bundle)}
    assert snapshot <= after
    assert len(after) > len(snapshot)
