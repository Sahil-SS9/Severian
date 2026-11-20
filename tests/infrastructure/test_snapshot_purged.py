"""M1 — the snapshot validator accepts purged rows and nothing else with a NULL key.

Owner decision (c): a purged row carries the fixed removal marker, hashes recomputed
over that marker, an empty payload-metadata surface and ``identity_key = NULL``. The
import path relaxes exactly those three things, and only for an explicitly purged
record — so a ``NULL`` identity key can no longer ride in as a generic bypass, and a
forged ``purged`` flag still has to satisfy every other check.

Records are built through the domain models so the payload can never drift from the
schema. No store is opened here: ``validate_snapshot`` is the unit under test.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from severian.domain.models import (
    ERASED,
    Entity,
    Memory,
    Observation,
    Relation,
    Scope,
)
from severian.infrastructure.snapshot import identity_hash, validate_snapshot

SCOPE = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
SCOPE_DIMENSIONS = ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")
# The fixed removal marker (M1 owner decision (c)), as a literal so this module collects
# on a tree without the production constant; pinned to that constant below.
PURGED_TEXT = "[purged]"
MARKER_HASH = hashlib.sha256(PURGED_TEXT.encode()).hexdigest()


def test_the_removal_marker_is_the_fixed_literal() -> None:
    from severian.domain.models import PURGE_MARKER

    assert PURGE_MARKER == PURGED_TEXT


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _item(
    record_type: str,
    model: Any,
    *,
    identity_key: str | None = None,
    content_hash: str | None = None,
) -> dict[str, Any]:
    """One export-shaped record row (only the fields the validator reads)."""
    return {
        "id": model.id,
        "record_type": record_type,
        **{dimension: getattr(model.scope, dimension) for dimension in SCOPE_DIMENSIONS},
        "content_hash": content_hash,
        "identity_key": identity_key,
        "status": None,
        "payload": model.model_dump(mode="json"),
    }


def _live_observation(text: str, **overrides: Any) -> Observation:
    """An ordinary, active observation: correct digests over its own text, no purge flag."""
    base: dict[str, Any] = {
        "content": text,
        "content_hash": _text_hash(text),
        "raw_content": text,
        "raw_hash": _text_hash(text),
    }
    base.update(overrides)
    return Observation(scope=SCOPE, **base)


def _live_item(text: str, *, identity_key: str | None = "") -> dict[str, Any]:
    observation = _live_observation(text)
    key = identity_hash(observation.content_hash) if identity_key == "" else identity_key
    return _item("observation", observation, content_hash=observation.content_hash, identity_key=key)


def _purged_observation(**overrides: Any) -> Observation:
    base: dict[str, Any] = {
        "content": PURGED_TEXT,
        "content_hash": MARKER_HASH,
        "raw_content": PURGED_TEXT,
        "raw_hash": MARKER_HASH,
        "source": {},
        "purged": True,
        "superseded_by_id": ERASED,
    }
    base.update(overrides)
    return Observation(scope=SCOPE, **base)


def _purged_memory(**overrides: Any) -> Memory:
    base: dict[str, Any] = {
        "kind": "fact",
        "content": PURGED_TEXT,
        "source_observation_ids": ["obs-1"],
        "model_id": "m",
        "prompt_version": "v",
        "confidence": 0.0,
        "superseded_by_id": ERASED,
        "purged": True,
    }
    base.update(overrides)
    return Memory(scope=SCOPE, **base)


def _purged_entity(**overrides: Any) -> Entity:
    base: dict[str, Any] = {"kind": "org", "name": PURGED_TEXT, "purged": True}
    base.update(overrides)
    return Entity(scope=SCOPE, **base)


# -- what a purge writes is importable ----------------------------------------


def test_a_purged_observation_with_the_marker_and_no_identity_validates() -> None:
    observation = _purged_observation()
    validate_snapshot([_item("observation", observation, content_hash=MARKER_HASH)])


def test_a_purged_memory_with_the_marker_and_no_identity_validates() -> None:
    observation = _purged_observation()
    memory = _purged_memory(source_observation_ids=[observation.id])
    validate_snapshot(
        [_item("observation", observation, content_hash=MARKER_HASH), _item("memory", memory)]
    )


def test_a_purged_entity_with_the_marker_and_no_identity_validates() -> None:
    entity = _purged_entity()
    validate_snapshot([_item("entity", entity)])


def test_a_snapshot_written_before_m1_still_validates() -> None:
    """Backward compatibility: pre-M1 payloads have no ``purged`` key at all."""
    item = _live_item("Ada founded a company")
    item["payload"].pop("purged")
    validate_snapshot([item])


# -- the flag is not a bypass -------------------------------------------------


def test_a_purged_observation_that_still_carries_text_is_rejected() -> None:
    text = "The pantry code is ZQX-9F3A"
    observation = _purged_observation(content=text, content_hash=_text_hash(text))
    item = _item("observation", observation, content_hash=observation.content_hash)
    with pytest.raises(ValueError, match="removal marker"):
        validate_snapshot([item])


def test_a_purged_observation_that_keeps_its_source_metadata_is_rejected() -> None:
    observation = _purged_observation(source={"uri": "note.txt"})
    item = _item("observation", observation, content_hash=MARKER_HASH)
    with pytest.raises(ValueError, match="source"):
        validate_snapshot([item])


def test_a_purged_observation_that_keeps_attachments_is_rejected() -> None:
    attachments = [{"kind": "file", "uri": "note.txt", "transcript": "the code"}]
    observation = _purged_observation(attachments=attachments)
    item = _item("observation", observation, content_hash=MARKER_HASH)
    with pytest.raises(ValueError, match="attachments"):
        validate_snapshot([item])


def test_a_purged_memory_that_keeps_cues_is_rejected() -> None:
    memory = _purged_memory(cues=["when they mention the pantry"])
    with pytest.raises(ValueError, match="cues"):
        validate_snapshot([_item("memory", memory)])


def test_a_purged_memory_that_keeps_a_trigger_is_rejected() -> None:
    memory = _purged_memory(trigger={"keywords": ["pantry"]})
    with pytest.raises(ValueError, match="trigger"):
        validate_snapshot([_item("memory", memory)])


def test_a_purged_entity_that_keeps_its_name_is_rejected() -> None:
    entity = _purged_entity(name="Acme")
    with pytest.raises(ValueError, match="removal marker"):
        validate_snapshot([_item("entity", entity)])


def test_a_purged_record_may_not_keep_its_identity_key() -> None:
    observation = _purged_observation()
    item = _item(
        "observation",
        observation,
        content_hash=MARKER_HASH,
        identity_key=identity_hash("anything"),
    )
    with pytest.raises(ValueError, match="identity"):
        validate_snapshot([item])


def test_a_purged_record_may_not_be_an_active_head() -> None:
    observation = _purged_observation(superseded_by_id=None)
    item = _item("observation", observation, content_hash=MARKER_HASH)
    with pytest.raises(ValueError, match="superseded"):
        validate_snapshot([item])


# -- NULL identity is not a generic bypass ------------------------------------


def test_a_live_observation_may_not_drop_its_identity_key() -> None:
    with pytest.raises(ValueError, match="identity"):
        validate_snapshot([_live_item("Ada founded a company", identity_key=None)])


def test_a_live_memory_may_not_drop_its_identity_key() -> None:
    memory = Memory(
        scope=SCOPE,
        kind="fact",
        content="Ada founded a company",
        source_observation_ids=["obs-1"],
        model_id="m",
        prompt_version="v",
        confidence=1.0,
    )
    with pytest.raises(ValueError, match="identity"):
        validate_snapshot([_item("memory", memory, identity_key=None)])


def test_a_live_entity_may_not_drop_its_identity_key() -> None:
    entity = Entity(scope=SCOPE, kind="org", name="Acme")
    with pytest.raises(ValueError, match="identity"):
        validate_snapshot([_item("entity", entity, identity_key=None)])


def test_a_forged_purged_flag_on_an_otherwise_valid_record_is_rejected() -> None:
    """A row cannot be declared purged without actually being scrubbed."""
    observation = _live_observation("Ada founded a company", purged=True, superseded_by_id=ERASED)
    item = _item("observation", observation, content_hash=observation.content_hash)
    with pytest.raises(ValueError, match="removal marker"):
        validate_snapshot([item])


def test_an_erased_record_may_release_its_identity_key() -> None:
    """M1 owner decision (a): erasure releases dedupe, so its key is legitimately NULL."""
    observation = _live_observation("Ada founded a company", superseded_by_id=ERASED)
    item = _item("observation", observation, content_hash=observation.content_hash, identity_key=None)
    validate_snapshot([item])


def test_the_purged_flag_is_the_only_missing_key_tolerated() -> None:
    """The pre-M1 tolerance is one key wide: anything else missing still fails closed."""
    item = _live_item("Ada founded a company")
    item["payload"].pop("purged")
    item["payload"].pop("raw_hash")
    with pytest.raises(ValueError, match="round-trip"):
        validate_snapshot([item])


def test_the_marker_hash_is_still_recomputed_for_a_purged_row() -> None:
    observation = _purged_observation(content_hash=MARKER_HASH)
    item = _item("observation", observation, content_hash=_text_hash("something else"))
    with pytest.raises(ValueError, match="content hash"):
        validate_snapshot([item])


def test_a_purged_observation_exports_back_to_the_same_payload() -> None:
    observation = _purged_observation()
    item = _item("observation", observation, content_hash=MARKER_HASH)
    assert json.loads(json.dumps(item["payload"])) == observation.model_dump(mode="json")


# -- M1 correction: historical provenance on relations ------------------------


def _graph_items(*, omit_the_field: bool = False) -> list[dict[str, Any]]:
    """Two entities, one observation and the relation between them, export-shaped."""
    observation = _live_observation("Ada works at Acme")
    alice = Entity(scope=SCOPE, kind="person", name="Ada")
    acme = Entity(scope=SCOPE, kind="org", name="Acme")
    relation = Relation(scope=SCOPE, subject_id=alice.id, predicate="works_at", object_id=acme.id)
    relation_item = _item(
        "relation",
        relation,
        identity_key=identity_hash("relation", alice.id, "works_at", acme.id),
    )
    if omit_the_field:
        relation_item["payload"].pop("retracted_for_ids", None)
    return [
        _item(
            "observation",
            observation,
            content_hash=observation.content_hash,
            identity_key=identity_hash(observation.content_hash or ""),
        ),
        _item("entity", alice, identity_key=identity_hash("person", "ada")),
        _item("entity", acme, identity_key=identity_hash("org", "acme")),
        relation_item,
    ]


def _observation_id(items: list[dict[str, Any]]) -> str:
    return str(next(item for item in items if item["record_type"] == "observation")["id"])


def test_a_snapshot_written_before_the_provenance_field_still_validates() -> None:
    """A store exported before the field existed carries no ``retracted_for_ids`` key.

    Its absence means "no evidence of a withdrawn support", exactly like a missing
    ``purged`` flag means ``False``: the one tolerated missing key, never a licence for
    any other missing or extra one.
    """
    validate_snapshot(_graph_items(omit_the_field=True))


def test_a_snapshot_that_moves_an_active_support_into_history_is_rejected() -> None:
    """A support cannot be both current and withdrawn: that would make the purge lie."""
    items = _graph_items()
    observation_id = _observation_id(items)
    items[-1]["payload"]["source_observation_ids"] = [observation_id]
    items[-1]["payload"]["retracted_for_ids"] = [observation_id]

    with pytest.raises(ValueError, match="both a support and a withdrawn support"):
        validate_snapshot(items)


def test_historical_provenance_that_names_no_observation_is_rejected() -> None:
    """Forged provenance would let a snapshot widen what a later purge scrubs."""
    items = _graph_items()
    items[-1]["payload"]["retracted_for_ids"] = ["no-such-observation"]

    with pytest.raises(ValueError, match="retracted_for_ids references a non-observation"):
        validate_snapshot(items)


def test_historical_provenance_from_another_scope_is_rejected() -> None:
    items = _graph_items()
    other = _live_observation("Ada works at Acme").model_copy(
        update={"scope": SCOPE.model_copy(update={"session_id": "s2"})}
    )
    items.append(
        _item(
            "observation",
            other,
            content_hash=other.content_hash,
            identity_key=identity_hash(other.content_hash or ""),
        )
    )
    items[-2]["payload"]["retracted_for_ids"] = [other.id]

    with pytest.raises(ValueError, match="different scope"):
        validate_snapshot(items)
