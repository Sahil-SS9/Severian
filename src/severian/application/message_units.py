"""Message-level indexing units (S1, ``SEVERIAN_MESSAGE_UNITS``).

A turn is one observation, but the answerer needs the user's line separately
from the assistant's reply. With the switch on, derivation emits one
``message_unit`` memory per role (and per sentence-boundary chunk for long
messages), linked to the parent observation. The unit content carries its role
and stable message order, so same-date units can be ordered without a schema
change. With the switch off nothing here runs.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, cast

from severian.domain.models import Memory, Observation, Scope

MESSAGE_UNIT_KIND = "message_unit"
MESSAGE_UNIT_MODEL = "severian.message-units"
MESSAGE_UNIT_PROMPT = "v1"
MAX_UNIT_CHARS = 1_200
UNIT_OVERLAP_CHARS = 120

_SWITCH = "SEVERIAN_MESSAGE_UNITS"
_DATE = re.compile(r"^(\[[^\]\n]{1,80}\]\s*)")
_ROLE = re.compile(r"(?m)^(user|assistant):\s*")
_ORDER = re.compile(r"\bmessage (\d+):")
_UNIT_DATE = re.compile(r"^\[(\d{4}/\d{2}/\d{2})")


def message_units_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``SEVERIAN_MESSAGE_UNITS=1`` enables the feature; unset/anything else is off."""
    values = os.environ if env is None else env
    return values.get(_SWITCH, "").strip().lower() in {"1", "true", "yes", "on"}


def unit_order(content: str) -> int | None:
    match = _ORDER.search(content)
    return int(match.group(1)) if match else None


def unit_date(content: str) -> str | None:
    match = _UNIT_DATE.match(content)
    return match.group(1) if match else None


def _message_order(observation: Observation, role: str, order_position: int | None = None) -> int:
    source = observation.source or {}
    explicit = source.get("message_order")
    if explicit is not None:
        try:
            return int(explicit)
        except (TypeError, ValueError):
            pass
    turn = source.get("turn")
    if turn is not None:
        try:
            return max(1, int(turn)) * 2 + (1 if role == "assistant" else 0)
        except (TypeError, ValueError):
            pass
    if order_position is not None:
        return max(1, order_position) * 2 + (1 if role == "assistant" else 0)
    return 2 + (1 if role == "assistant" else 0)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [part.strip() for part in parts if part.strip()] or [text.strip()]


def _chunks(text: str) -> list[str]:
    """Sentence-boundary chunks; an oversized single sentence is split at a word boundary."""
    chunks: list[str] = []
    current: list[str] = []
    for sentence in _sentences(text):
        pieces = [sentence]
        if len(sentence) > MAX_UNIT_CHARS:
            pieces = []
            remaining = sentence
            while len(remaining) > MAX_UNIT_CHARS:
                cut = remaining.rfind(" ", 0, MAX_UNIT_CHARS)
                if cut <= 0:
                    cut = MAX_UNIT_CHARS
                pieces.append(remaining[:cut].strip())
                remaining = remaining[cut:].strip()
            if remaining:
                pieces.append(remaining)
        for piece in pieces:
            candidate = " ".join([*current, piece])
            if current and len(candidate) > MAX_UNIT_CHARS:
                chunks.append(" ".join(current))
                overlap: list[str] = []
                for previous in reversed(current):
                    if sum(len(item) + 1 for item in overlap) + len(previous) > UNIT_OVERLAP_CHARS:
                        break
                    overlap.insert(0, previous)
                current = [*overlap, piece]
            else:
                current.append(piece)
    if current:
        chunks.append(" ".join(current))
    return chunks


def _roles(observation: Observation) -> list[tuple[str, str]]:
    """(role, text) from ``user:``/``assistant:`` markers, or one source-declared role."""
    body = observation.content
    match = _DATE.match(body)
    if match:
        body = body[match.end() :]
    markers = list(_ROLE.finditer(body))
    if not markers:
        role = str((observation.source or {}).get("speaker") or "user")
        return [("assistant" if role == "assistant" else "user", body.strip())]
    out: list[tuple[str, str]] = []
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(body)
        text = body[marker.end() : end].strip()
        if text:
            out.append((marker.group(1), text))
    return out


def message_unit_memories(
    observation: Observation,
    scope: Scope | None = None,
    *,
    enabled: bool | None = None,
    order_position: int | None = None,
) -> list[Memory]:
    """Deterministic unit memories for one turn; empty when the switch is off."""
    if enabled is None:
        enabled = message_units_enabled()
    if not enabled:
        return []
    target_scope = scope or observation.scope
    date = ""
    match = _DATE.match(observation.content)
    if match:
        date = match.group(1).strip()
    units: list[Memory] = []
    for role, text in _roles(observation):
        base_order = _message_order(observation, role, order_position)
        chunks = _chunks(text)
        for part, chunk in enumerate(chunks, 1):
            part_label = f" part {part}" if len(chunks) > 1 else ""
            content = f"{date + ' ' if date else ''}{role} message {base_order}{part_label}: {chunk}"
            units.append(
                Memory(
                    scope=target_scope,
                    kind=MESSAGE_UNIT_KIND,
                    content=content,
                    source_observation_ids=[observation.id],
                    model_id=MESSAGE_UNIT_MODEL,
                    prompt_version=MESSAGE_UNIT_PROMPT,
                    confidence=1.0,
                )
            )
    return units


def prefer_message_units(results: Sequence[Any]) -> list[Any]:
    """Drop a turn's generic derived memory when its units are in the same result set.

    User facts are kept: they are structured evidence, not a duplicate of the turn.
    """
    sources = {
        source
        for item in results
        if getattr(item.memory, "kind", "") == MESSAGE_UNIT_KIND
        for source in item.source_observation_ids
    }
    if not sources:
        return list(results)
    out: list[Any] = []
    for item in results:
        if getattr(item.memory, "kind", "") in {MESSAGE_UNIT_KIND, "user_fact"}:
            out.append(item)
        elif not (set(item.source_observation_ids) & sources):
            out.append(item)
    return out


def _unit_positions(hits: Sequence[Any]) -> dict[str, list[int]]:
    positions: dict[str, list[int]] = {}
    for index, hit in enumerate(hits):
        order = unit_order(getattr(hit, "content", ""))
        date = unit_date(getattr(hit, "content", ""))
        if order is None or date is None:
            continue
        positions.setdefault(date, []).append(index)
    return positions


def order_unit_hits(hits: Sequence[Any]) -> list[Any]:
    """Stable same-date ordering: units of one date sort by message order in place."""
    ordered = list(hits)
    for indices in _unit_positions(hits).values():
        if len(indices) < 2:
            continue
        sorted_hits = sorted((hits[index] for index in indices), key=lambda hit: unit_order(hit.content) or 0)
        for index, hit in zip(indices, sorted_hits, strict=True):
            ordered[index] = hit
    return ordered


def mark_later_units(hits: Sequence[Any]) -> dict[str, str]:
    """Content for rendering: every unit after the first on its date says ``[later message]``."""
    texts: dict[str, str] = {}
    for indices in _unit_positions(hits).values():
        ordered = sorted((hits[index] for index in indices), key=lambda hit: unit_order(hit.content) or 0)
        first_order = unit_order(ordered[0].content) if ordered else None
        for hit in ordered:
            content = hit.content
            order = unit_order(content)
            texts[str(getattr(hit, "memory_id", id(hit)))] = (
                content if order == first_order else f"{content} [later message]"
            )
    return texts


@dataclass(frozen=True)
class MessageUnitMigration:
    scope: dict[str, str]
    observations: int
    units_planned: int
    units_present: int
    units_written: int
    drift: int
    dry_run: bool
    rehearsal: dict[str, Any] | None = None
    backup: str | None = None
    integrity: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unit_count(repository: Any, scope: Scope) -> int:
    return sum(1 for memory in repository.list_memories(scope) if memory.kind == MESSAGE_UNIT_KIND)


def migrate_message_units(
    repository: Any,
    scope: Scope,
    *,
    confirm: bool = False,
    rebuild: Any | None = None,
) -> MessageUnitMigration:
    """Additive, idempotent unit backfill for one scope; dry-run unless ``confirm``.

    Only active observations get units. Existing units are left alone (the
    memory identity is deterministic), so a second apply writes nothing.
    """
    observations = [obs for obs in repository.list_observations(scope) if obs.superseded_by_id is None]
    positions_fn = getattr(repository, "observation_positions", None)
    positions = cast(dict[str, int], positions_fn(scope)) if callable(positions_fn) else {}
    planned = sum(
        len(message_unit_memories(obs, scope, enabled=True, order_position=positions.get(obs.id)))
        for obs in observations
    )
    present = _unit_count(repository, scope)
    written = 0
    if confirm:
        before = present
        all_units: list[Memory] = []
        for observation in observations:
            units = message_unit_memories(
                observation, scope, enabled=True, order_position=positions.get(observation.id)
            )
            if units:
                all_units.extend(units)
                repository.publish_derivation(
                    scope,
                    entities=[],
                    relations=[],
                    memories=units,
                    source_observation_id=observation.id,
                )
        if rebuild is not None and all_units:
            # Index only the units; a full scope rebuild would re-encode every
            # existing memory and turn a backfill into an hours-long model job.
            rebuild.lexical.upsert_many(all_units)
            rebuild.vector.upsert_many(all_units)
        written = max(0, _unit_count(repository, scope) - before)
    after = present + written
    return MessageUnitMigration(
        scope=scope.model_dump(),
        observations=len(observations),
        units_planned=planned,
        units_present=after,
        units_written=written,
        drift=max(0, planned - after),
        dry_run=not confirm,
    )

