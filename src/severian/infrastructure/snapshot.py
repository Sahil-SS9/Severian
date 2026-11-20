"""Strict semantic validation for Severian v1 snapshots (import path).

The import path must not trust the snapshot. A checksum proves nothing
when the attacker can recompute it (SHA-256 is unkeyed). This module
revalidates every record semantically before any write:

1. Parse each record with the domain model and require an exact
   round-trip (``model_dump(mode="json") == payload``) — equivalent to
   ``extra="forbid"`` plus strict typing for every field.
2. Recompute every derived field (content hash, identity key) and
   require equality with the exported columns. Relations have TWO writer
   schemes (the derive path keys ``("relation", s, p, o)``; ``add_relation``
   keys ``(s, p, o, valid_from, valid_to)`` with datetime objects); a key is
   accepted only if it matches one of them exactly. Task notes key only their
   ``current`` version; retired versions must carry no key.
3. Verify first-class scope columns match the payload's scope
   (forging ``tenant_id`` in the column while the payload names another
   scope is rejected).
4. Validate references (memory -> source observations, relation ->
   entity endpoints) and revision topology (``revision_of_id`` /
   ``superseded_by_id`` point at existing records of the same kind).
5. Validate status values and collection/tenant coherence.

**Purged rows (M1).** ``observation``, ``memory`` and ``entity`` payloads carry a
``purged`` boolean. The validator relaxes exactly three things, and only when it is
true: the text fields must equal the fixed removal marker (their digests are still
recomputed and compared), the payload metadata that could hold text (``source``,
``attachments``, ``cues``, ``trigger``) must be empty, and ``identity_key`` must be
``NULL``. A ``NULL`` identity key on a record that is *not* purged is rejected — that
is what stops "purged" from becoming a generic way to opt out of dedupe and of this
check. A snapshot written before M1 has no ``purged`` key; that single missing key is
tolerated (its model default is ``False``) and nothing else is.

All checks run before the write transaction opens. Any mismatch raises
``ValueError``; the caller's transaction never starts.

Design note: the identity hash encoding is the same canonical
``json.dumps(sort_keys, separators)`` used by both backends so a
legitimate export always revalidates cleanly.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from severian.domain.models import (
    ERASED,
    PURGE_MARKER,
    AuditEvent,
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
    TaskNote,
    source_identity,
)

SCOPE_DIMENSIONS = ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")

RECORD_MODELS: dict[str, type[Any]] = {
    "collection": Collection,
    "observation": Observation,
    "memory": Memory,
    "entity": Entity,
    "relation": Relation,
    "job": Job,
    "outbox": Event,
    "audit": AuditEvent,
    "task_note": TaskNote,
}

JOB_STATUSES = {"pending", "running", "done", "failed", "dead_letter"}
OUTBOX_STATUSES = {"pending", "acked"}
TASK_NOTE_STATUSES = {"current", "replaced", "cleared"}

# Record kinds that carry the M1 ``purged`` flag. Only for these is a missing key
# tolerated (a snapshot written before M1 predates the field), and only for a row whose
# flag is true are the text/identity checks relaxed.
_PURGE_AWARE_KINDS = frozenset({"observation", "memory", "entity"})
# M1 correction (F1): ``Relation.retracted_for_ids`` is a later payload key, so a snapshot
# written before it has none. Its absence means "no evidence of a withdrawn support" - the
# empty default - and is the only missing key tolerated on a relation.
RELATION_PROVENANCE_KEY = "retracted_for_ids"
# Payload metadata that can carry text: a purged row must have emptied it.
_PURGE_EMPTY_FIELDS: dict[str, tuple[tuple[str, Any], ...]] = {
    "observation": (("source", {}), ("attachments", [])),
    "memory": (("cues", []), ("trigger", None)),
}


def _purged_problem(kind: str, payload: dict[str, Any], key: str | None) -> str | None:
    """Why a row claiming ``purged`` is not actually scrubbed, or ``None`` when it is.

    The flag is never a bypass: a purged row must carry the fixed removal marker in every
    text field (so its digests are the marker's digests), must have emptied the payload
    metadata that could hold text, must have released its identity key, and must not be an
    active head. Everything else about the record is validated exactly as before.
    """
    if kind == "entity":
        if payload.get("name") != PURGE_MARKER:
            return "purged record must carry the removal marker as its name"
    else:
        text_fields = ("content", "raw_content") if kind == "observation" else ("content",)
        for field in text_fields:
            if payload.get(field) != PURGE_MARKER:
                return "purged record must carry the removal marker, not text"
    for field, empty in _PURGE_EMPTY_FIELDS.get(kind, ()):
        if payload.get(field) != empty:
            return f"purged record must not keep its {field}"
    if key is not None:
        return "purged record must release its identity key"
    if kind != "entity" and payload.get("superseded_by_id") is None:
        return "purged record must never be an active head (superseded_by_id is set)"
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def identity_hash(*parts: object) -> str:
    """Canonical identity hash shared by snapshot validation."""
    return hashlib.sha256(_canonical_json(parts).encode()).hexdigest()


def _as_datetime(value: Any) -> datetime | None:
    """Parse a payload validity value back to the object the writer hashed.

    Writers pass ``datetime`` objects through ``json.dumps(default=str)``
    (space-separated ``str(dt)``); the payload carries pydantic ISO strings
    (T-separated). Hashing the raw string would not reproduce the writer's
    key for a dated relation, so parse first. ``None`` stays ``None``.
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _relation_identities(payload: dict[str, Any]) -> tuple[str, str]:
    """Both relation writer schemes, each recomputed exactly (M0 D1).

    - derive path (``publish_derivation``): ``("relation", s, p, o)``;
    - manual path (``add_relation``): ``(s, p, o, valid_from, valid_to)`` with
      datetime objects, matching the writer's ``default=str`` encoding.

    A key matching neither scheme is forged and is rejected. No existing row
    is rewritten and dedupe behaviour is unchanged.
    """
    subject = payload["subject_id"]
    predicate = payload["predicate"]
    obj = payload["object_id"]
    derive = identity_hash("relation", subject, predicate, obj)
    manual = identity_hash(
        subject,
        predicate,
        obj,
        _as_datetime(payload.get("valid_from")),
        _as_datetime(payload.get("valid_to")),
    )
    return derive, manual


def _payload_round_trips(dumped: dict[str, Any], payload: dict[str, Any]) -> bool:
    """Exact model round-trip, tolerating only equivalent ISO datetime encodings.

    Some writers store a datetime via ``datetime.isoformat()`` ("+00:00")
    while pydantic dumps UTC as "Z" — task-note ``replaced_at`` (``_retire``)
    and closed-memory ``valid_until`` (``close_memory``). Both are the same
    instant; byte-equality rejected every store holding such a record (M0
    root causes 4 and 7). The key sets must match exactly and every other
    field must be byte-equal, so no extra/unknown/derived field slips through.
    """
    if dumped == payload:
        return True
    if set(dumped) != set(payload):
        return False
    for key, value in dumped.items():
        other = payload.get(key)
        if value == other:
            continue
        if isinstance(value, str) and isinstance(other, str):
            try:
                if datetime.fromisoformat(value) == datetime.fromisoformat(other):
                    continue
            except ValueError:
                pass
        return False
    return True


def _recompute_identity(kind: str, payload: dict[str, Any]) -> str | None:
    """Recompute the identity key a write would have produced.

    Returns ``None`` for record kinds where the backend stores no
    identity key (revisions, outbox, audit). Relations and task notes are
    handled in ``_validate_record`` (two schemes / status-dependent key).
    """
    kind_value = str(kind)
    if kind_value == "collection":
        return identity_hash(payload["id"])
    if kind_value == "observation":
        if payload.get("revision_of_id"):
            return identity_hash("revision", payload["revision_of_id"], payload["id"])
        digest = hashlib.sha256(str(payload["content"]).encode()).hexdigest()
        source = source_identity(dict(payload.get("source") or {}))
        if source:
            return identity_hash(digest, source)
        return identity_hash(digest)
    if kind_value == "memory":
        if payload.get("revision_of_id"):
            return None
        return identity_hash(
            payload["kind"],
            payload["content"],
            payload["source_observation_ids"],
            payload["model_id"],
            payload["prompt_version"],
        )
    if kind_value == "entity":
        return identity_hash(payload["kind"], str(payload["name"]).casefold())
    if kind_value == "job":
        return identity_hash(payload["kind"], payload["payload"])
    return None


def _validate_record(item: dict[str, Any], index: int) -> dict[str, Any]:
    """Validate one snapshot record and return its parsed payload."""
    if not isinstance(item, dict):
        raise ValueError(f"record {index} is not an object")
    raw_kind = item.get("record_type")
    kind = str(raw_kind) if raw_kind is not None else ""
    model = RECORD_MODELS.get(kind)
    if model is None:
        raise ValueError(f"record {index}: invalid record_type {raw_kind!r}")
    payload = item.get("payload")
    if not isinstance(payload, dict):
        raise ValueError(f"record {index}: payload is not an object")
    # A snapshot written before M1 has no ``purged`` key at all. Its absence means
    # ``False``, so the one tolerated missing key is inserted before the round-trip
    # comparison; every other missing or extra key still fails closed.
    if kind in _PURGE_AWARE_KINDS and "purged" not in payload:
        payload = {**payload, "purged": False}
    if kind == "relation" and RELATION_PROVENANCE_KEY not in payload:
        payload = {**payload, RELATION_PROVENANCE_KEY: []}
    try:
        parsed = model.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"record {index}: invalid {kind} payload: {exc.errors()[0].get('msg')}") from exc
    dumped = parsed.model_dump(mode="json")
    if not _payload_round_trips(dumped, payload):
        raise ValueError(f"record {index}: {kind} payload does not round-trip (extra/unknown or derived fields)")
    if parsed.id != item.get("id"):
        raise ValueError(f"record {index}: payload metadata id mismatch")

    # Scope coherence: first-class scope columns must equal the payload's
    # scope. A snapshot that moves a record between scopes by editing the
    # columns (and recomputing the checksum) is rejected.
    if kind == "collection":
        if parsed.tenant_id != item.get("tenant_id"):
            raise ValueError(f"record {index}: payload metadata tenant mismatch")
        if parsed.id != item.get("collection_id"):
            raise ValueError(f"record {index}: payload metadata collection mismatch")
    else:
        if not hasattr(parsed, "scope"):
            raise ValueError(f"record {index}: {kind} payload missing scope")
        for dimension in SCOPE_DIMENSIONS:
            column = item.get(dimension)
            payload_value = getattr(parsed.scope, dimension)
            if column != payload_value:
                raise ValueError(f"record {index}: payload metadata scope mismatch ({dimension})")

    # Content hash recomputation (observations only).
    if kind == "observation":
        expected = hashlib.sha256(parsed.content.encode()).hexdigest()
        if item.get("content_hash") != expected or parsed.content_hash != expected:
            raise ValueError(f"record {index}: payload metadata content hash mismatch")
        # Raw evidence hash must match the raw content when present.
        if parsed.raw_content:
            expected_raw = hashlib.sha256(parsed.raw_content.encode()).hexdigest()
            if parsed.raw_hash != expected_raw:
                raise ValueError(f"record {index}: payload metadata raw hash mismatch")

    # Identity key recomputation. Relations accept either writer scheme, each
    # recomputed exactly; task notes key only the current version (a key on a
    # retired version could masquerade as current, since the note lookup is by
    # key, not status). All other kinds use the single write-path scheme.
    key = item.get("identity_key")
    purged = bool(payload.get("purged")) if kind in _PURGE_AWARE_KINDS else False
    if purged:
        problem = _purged_problem(kind, payload, key)
        if problem is not None:
            raise ValueError(f"record {index}: {problem}")
    if kind == "relation":
        if key is not None and key not in _relation_identities(payload):
            raise ValueError(f"record {index}: payload metadata identity mismatch")
    elif kind == "task_note":
        if item.get("status") == "current":
            if key != identity_hash("task_note", payload["task"]):
                raise ValueError(f"record {index}: payload metadata identity mismatch")
        elif key is not None:
            raise ValueError(f"record {index}: payload metadata identity mismatch")
    elif not purged:
        expected_identity = _recompute_identity(kind, payload)
        if expected_identity is not None and key != expected_identity:
            # A NULL key is legitimate only on a record that is not current evidence: a
            # purged row (checked above) or one that has been superseded/erased, whose key
            # the erasure path releases so the same text can be ingested again as new
            # evidence (M1 owner decision (a)). On an ACTIVE record it would silently opt
            # out of dedupe and out of this check, which is the bypass M1 closes.
            superseded = kind != "entity" and payload.get("superseded_by_id") is not None
            if key is not None or not superseded:
                raise ValueError(f"record {index}: payload metadata identity mismatch")

    # Status values.
    if kind == "job" and item.get("status") not in JOB_STATUSES:
        raise ValueError(f"record {index}: invalid job status {item.get('status')!r}")
    if kind == "outbox" and item.get("status") not in OUTBOX_STATUSES:
        raise ValueError(f"record {index}: invalid outbox status {item.get('status')!r}")
    if kind == "task_note" and item.get("status") not in TASK_NOTE_STATUSES:
        raise ValueError(f"record {index}: invalid task_note status {item.get('status')!r}")

    return payload


def _record_scope(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Extract the 5-D scope of a snapshot record item.

    Records carry the scope as top-level columns (tenant_id, profile_id,
    collection_id, agent_id, session_id) alongside the payload.
    """
    return (
        str(item.get("tenant_id", "")),
        str(item.get("profile_id", "")),
        str(item.get("collection_id", "")),
        str(item.get("agent_id", "")),
        str(item.get("session_id", "")),
    )


def validate_snapshot(incoming: list[dict[str, Any]]) -> None:
    """Semantically validate an entire snapshot before import.

    Raises ``ValueError`` on the first invalid record. After this
    returns, the snapshot's records are safe to stage.
    """
    by_id: dict[str, dict[str, Any]] = {}
    parsed_payloads: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(incoming):
        payload = _validate_record(item, index)
        by_id[str(item["id"])] = item
        parsed_payloads[str(item["id"])] = payload

    # Reference and revision topology validation.
    for record_id, item in by_id.items():
        kind = str(item["record_type"])
        payload = parsed_payloads[record_id]
        if kind in ("observation", "memory"):
            for link_field in ("revision_of_id", "superseded_by_id"):
                target = payload.get(link_field)
                if target is None:
                    continue
                # Erasure tombstones are born superseded by the ERASED marker:
                # a terminal state, not a reference to a record (M0 root cause 5).
                if link_field == "superseded_by_id" and target == ERASED:
                    continue
                # Correction-propagation marker (supersede_memories_for_source):
                # the memory is stale because its source observation was revised.
                # The suffix must name an existing same-scope observation; a
                # forged marker is rejected (M0 root cause 6).
                if link_field == "superseded_by_id" and isinstance(target, str) and target.startswith("source-rev:"):
                    source_record = by_id.get(target[len("source-rev:"):])
                    if source_record is None or str(source_record["record_type"]) != "observation":
                        raise ValueError(
                            f"record {record_id}: superseded_by_id source-rev marker references no observation"
                        )
                    if _record_scope(source_record) != _record_scope(item):
                        raise ValueError(
                            f"record {record_id}: superseded_by_id source-rev marker is in a different scope"
                        )
                    continue
                target_record = by_id.get(str(target))
                if target_record is None or str(target_record["record_type"]) != kind:
                    raise ValueError(f"record {record_id}: {link_field} does not reference an existing {kind}")
        if kind == "memory":
            for source_id in payload.get("source_observation_ids", []):
                source = by_id.get(str(source_id))
                if source is None or str(source["record_type"]) != "observation":
                    raise ValueError(f"record {record_id}: source_observation_ids references a non-observation")
                # DATA-01: memory → observation references must be same-scope.
                if _record_scope(source) != _record_scope(item):
                    raise ValueError(
                        f"record {record_id}: source observation {source_id} is in a different scope"
                    )
        if kind == "relation":
            for endpoint in (payload.get("subject_id"), payload.get("object_id")):
                endpoint_record = by_id.get(str(endpoint))
                if endpoint_record is None or str(endpoint_record["record_type"]) != "entity":
                    raise ValueError(f"record {record_id}: relation endpoint references a non-entity")
                # DATA-01: relation → entity references must be same-scope.
                if _record_scope(endpoint_record) != _record_scope(item):
                    raise ValueError(
                        f"record {record_id}: relation endpoint {endpoint} is in a different scope"
                    )
            # M1 correction (F1): withdrawn-support provenance is what lets a purge reach a
            # relation an erasure already retracted. It is only believable when it names a
            # real observation in this relation's own scope, and a support cannot be current
            # and withdrawn at once - forged provenance would otherwise widen what a later
            # purge scrubs.
            for source_id in payload.get(RELATION_PROVENANCE_KEY, []):
                if str(source_id) in payload.get("source_observation_ids", []):
                    raise ValueError(
                        f"record {record_id}: {RELATION_PROVENANCE_KEY} names a record that is "
                        "both a support and a withdrawn support"
                    )
                source_record = by_id.get(str(source_id))
                if source_record is None or str(source_record["record_type"]) != "observation":
                    raise ValueError(
                        f"record {record_id}: {RELATION_PROVENANCE_KEY} references a non-observation"
                    )
                if _record_scope(source_record) != _record_scope(item):
                    raise ValueError(
                        f"record {record_id}: {RELATION_PROVENANCE_KEY} names an observation "
                        "in a different scope"
                    )
