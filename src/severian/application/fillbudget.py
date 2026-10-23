"""``SEVERIAN_FILL_BUDGET``: fill the spare budget from the conversation already shown.

Severian hands over the top ``limit`` items even when they leave most of the character window empty: on LoCoMo the
kept round-3 run averages 2.4k characters at an 8,000-character budget, so ten items fill under a third of it. Filling
that space finds evidence for more questions (LoCoMo: +12.2 points of "any evidence found", +9.7 of "gold answer words
in context" at 8,000 characters) without costing the ones it already found.

**Why this is not a wider search.** The first design raised the search limit and let assembly keep adding ranked
items. Measured, it failed both gates:

- *It moved the head.* The service sizes its own candidate pool from the caller's ``limit``, so asking for 40 items
  feeds fusion and the cross-encoder a different candidate set from asking for 10. On the questions that lost "all
  evidence found" at 4,000 characters, only 4 to 8 of the ten items survived. A deeper search cannot be additive.
- *It tripled search latency.* p95 went from 296 ms to 1,103 ms on BEAM and 100 ms to 206 ms on LoCoMo, against a gate
  of 1.10x or +10 ms; the cost curve is +35% at pool 15 and +184% at pool 40. No pool large enough to fill 8,000
  characters fits inside the gate.

**What it does instead.** It adds no search. It reads the turns either side of the items already shown, from the same
conversations, and appends them after those items. Two indexed reads per query (the session's turns, then the
memories of the neighbouring turns): no embedding, no rerank. The head cannot move, because the caller's items are
laid out first, unchanged and in order — with the switch off the block is byte-identical to 0.3.11, and with it on the
off block is a byte prefix of the new one.

``FILL_SEEDS`` bounds how many of the shown items are expanded and ``FILL_WINDOW`` how many turns each side are read.
Measured on the kept stores, three seeds and two turns either side capture the whole gain: a wider window or more
seeds added items without adding a point. Both are bounded so a long conversation can never be read whole.

With ``SEVERIAN_REPLY_TRIM`` on as well, trimming rewrites the text of items that no longer fit, so the prefix
guarantee then applies to the order only. Off by default.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol

from severian.application.retrieval import RetrievalResult
from severian.domain.models import Memory, Observation, Scope


class NeighbourReads(Protocol):
    """The only three reads the fill makes: what it needs, and nothing that could widen the scope."""

    def get_observation(self, scope: Scope, observation_id: str) -> Observation: ...

    def list_session_observations(self, scope: Scope, session_ids: list[str]) -> list[Observation]: ...

    def list_memories_for_observations(self, scope: Scope, observation_ids: list[str]) -> list[Memory]: ...


#: Turns either side of a shown item that may be appended.
FILL_WINDOW = 2
#: How many of the shown items are expanded, best-ranked first.
FILL_SEEDS = 3
#: Bounds: ten items with five turns each side is already 100 turns of a conversation.
MAX_WINDOW = 5
MAX_SEEDS = 10
#: Below this much spare room (characters) the fill does not even read: two frames and a few words cannot help.
MIN_FILL_ROOM = 160


def _turn_order(observation: Observation) -> tuple[int, float, str]:
    """Conversation order: the turn number when the source carries one, else save time, then id.

    The repository returns a session's observations in id order, which is not conversation order and would make
    "the turns either side" mean nothing.
    """
    raw = str(observation.source.get("turn", "")).strip()
    turn = int(raw) if raw.isdigit() else 0
    return (turn, observation.created_at.timestamp(), observation.id)


def fill_budget_enabled() -> bool:
    """``SEVERIAN_FILL_BUDGET`` switches the fill on. Off: the block is exactly as before."""
    return os.environ.get("SEVERIAN_FILL_BUDGET", "").strip().lower() in {"1", "true", "on", "yes"}


def _bounded(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return max(low, min(high, value))


def neighbour_window() -> int:
    """Turns either side of a shown item to read (``SEVERIAN_FILL_WINDOW``, 1 to 5)."""
    return _bounded("SEVERIAN_FILL_WINDOW", FILL_WINDOW, 1, MAX_WINDOW)


def fill_seeds() -> int:
    """How many of the shown items to expand (``SEVERIAN_FILL_SEEDS``, 1 to 10)."""
    return _bounded("SEVERIAN_FILL_SEEDS", FILL_SEEDS, 1, MAX_SEEDS)


def neighbour_memories(
    repository: NeighbourReads,
    scope: Scope,
    results: Sequence[RetrievalResult],
    *,
    window: int | None = None,
    seeds: int | None = None,
) -> list[Memory]:
    """Memories from the turns around the shown items, in conversation order, never one already shown.

    ``results`` are the items the caller is about to see, in rank order. Only the first ``seeds`` of them are expanded,
    and only the ``window`` turns either side of each, so the read is bounded by the size of those conversations'
    turns rather than by the store. Every read is scope-bound, so no other profile's conversation can appear.
    """
    window = neighbour_window() if window is None else window
    seeds = fill_seeds() if seeds is None else seeds
    picked = list(results)[:seeds]
    if not picked or window < 1:
        return []

    sessions: set[str] = set()
    for result in picked:
        for observation_id in result.memory.source_observation_ids:
            try:
                observation = repository.get_observation(scope, observation_id)
            except (LookupError, KeyError):
                continue
            session = str(observation.source.get("session_id", "")).strip()
            if session:
                sessions.add(session)
    if not sessions:
        return []

    turns: dict[str, list[Observation]] = {}
    for observation in repository.list_session_observations(scope, sorted(sessions)):
        session = str(observation.source.get("session_id", "")).strip()
        if session:
            turns.setdefault(session, []).append(observation)
    if not turns:
        return []
    for observations in turns.values():
        observations.sort(key=_turn_order)
    position = {
        observation.id: (session, index)
        for session, observations in turns.items()
        for index, observation in enumerate(observations)
    }

    wanted: list[str] = []  # seed rank, then conversation order around that seed
    for result in picked:
        for session, index in sorted(
            position[observation_id]
            for observation_id in result.memory.source_observation_ids
            if observation_id in position
        ):
            observations = turns[session]
            low = max(0, index - window)
            high = min(len(observations), index + window + 1)
            wanted.extend(observations[at].id for at in range(low, high) if at != index)
    if not wanted:
        return []

    by_observation: dict[str, list[Memory]] = {}
    for memory in repository.list_memories_for_observations(scope, sorted(set(wanted))):
        for observation_id in memory.source_observation_ids:
            by_observation.setdefault(observation_id, []).append(memory)

    shown = {result.memory.id for result in results}
    appended: list[Memory] = []
    for observation_id in wanted:
        for memory in by_observation.get(observation_id, ()):
            if memory.id in shown:
                continue
            shown.add(memory.id)
            appended.append(memory)
    return appended


__all__ = [
    "FILL_SEEDS",
    "FILL_WINDOW",
    "fill_budget_enabled",
    "fill_seeds",
    "neighbour_memories",
    "neighbour_window",
]
