"""Strict semantic validation for Severian v1 snapshots (import path).

The import path must not trust the snapshot. A checksum proves nothing
when the attacker can recompute it (SHA-256 is unkeyed). This module
revalidates every record semantically before any write:

1. Parse each record with the domain model and require an exact
   round-trip (``model_dump(mode="json") == payload``) — equivalent to
   ``extra="forbid"`` plus strict typing for every field.
2. Recompute every derived field (content hash, identity key) and
   require equality with the exported columns.
3. Verify first-class scope columns match the payload's scope
   (forging ``tenant_id`` in the column while the payload names another
   scope is rejected).
4. Validate references (memory -> source observations, relation ->
   entity endpoints) and revision topology (``revision_of_id`` /
   ``superseded_by_id`` point at existing records of the same kind).
5. Validate status values and collection/tenant coherence.

All checks run before the write transaction opens. Any mismatch raises
``ValueError``; the caller's transaction never starts.

Design note: the identity hash encoding is the same canonical
``json.dumps(sort_keys, separators)`` used by both backends so a
legitimate export always revalidates cleanly.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from severian.domain.models import (
    AuditEvent,
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
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
}

JOB_STATUSES = {"pending", "running", "done", "failed"}
OUTBOX_STATUSES = {"pending"}


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def identity_hash(*parts: object) -> str:
    """Canonical identity hash shared by snapshot validation."""
    return hashlib.sha256(_canonical_json(parts).encode()).hexdigest()


def _recompute_identity(kind: str, payload: dict[str, Any]) -> str | None:
    """Recompute the identity key a write would have produced.

    Returns ``None`` for record kinds where the backend stores no
    identity key (revisions, outbox, audit).
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
    if kind_value == "relation":
        return identity_hash(
            payload["subject_id"],
            payload["predicate"],
            payload["object_id"],
            payload.get("valid_from"),
            payload.get("valid_to"),
        )
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
    try:
        parsed = model.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(f"record {index}: invalid {kind} payload: {exc.errors()[0].get('msg')}") from exc
    if parsed.model_dump(mode="json") != payload:
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

    # Identity key recomputation.
    expected_identity = _recompute_identity(kind, payload)
    if item.get("identity_key") is not None and expected_identity is not None:
        if item["identity_key"] != expected_identity:
            raise ValueError(f"record {index}: payload metadata identity mismatch")

    # Status values.
    if kind == "job" and item.get("status") not in JOB_STATUSES:
        raise ValueError(f"record {index}: invalid job status {item.get('status')!r}")
    if kind == "outbox" and item.get("status") not in OUTBOX_STATUSES:
        raise ValueError(f"record {index}: invalid outbox status {item.get('status')!r}")

    return payload


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
                if target is not None:
                    target_record = by_id.get(str(target))
                    if target_record is None or str(target_record["record_type"]) != kind:
                        raise ValueError(f"record {record_id}: {link_field} does not reference an existing {kind}")
        if kind == "memory":
            for source_id in payload.get("source_observation_ids", []):
                source = by_id.get(str(source_id))
                if source is None or str(source["record_type"]) != "observation":
                    raise ValueError(f"record {record_id}: source_observation_ids references a non-observation")
        if kind == "relation":
            for endpoint in (payload.get("subject_id"), payload.get("object_id")):
                endpoint_record = by_id.get(str(endpoint))
                if endpoint_record is None or str(endpoint_record["record_type"]) != "entity":
                    raise ValueError(f"record {record_id}: relation endpoint references a non-entity")
