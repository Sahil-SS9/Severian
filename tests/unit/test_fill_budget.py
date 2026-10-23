"""SEVERIAN_FILL_BUDGET: fill the spare budget from the conversation already shown.

The switch must add text only where the budget has room, and must never move an item the caller already had: with it
on, the off block is a prefix of the new block. These tests pin the ordering and the window with a stub repository;
the behaviour through the real service is in ``tests/application/test_context_efficiency.py``.

Why this is not the first design: over-fetching the search (a bigger ``limit``) changed which items ranked top ten,
because the service sizes its own candidate pool from the caller's limit, and it tripled search latency. See
``evidence/REPORT.md`` and ``evidence/REVIEW-INDEPENDENT.md`` in ``~/data/benchmarks/sev-ctxeff-20261022``.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from severian.application.fillbudget import (
    FILL_SEEDS,
    FILL_WINDOW,
    fill_budget_enabled,
    fill_seeds,
    neighbour_memories,
    neighbour_window,
)
from severian.application.retrieval import RetrievalResult
from severian.domain.models import Memory, Observation, Scope

SCOPE = Scope(tenant_id="t", profile_id="alice", collection_id="profile", agent_id="a", session_id="s1")
START = datetime(2024, 3, 15, 9, 0, tzinfo=UTC)


def observation(turn: int, session: str = "s0001") -> Observation:
    return Observation(
        scope=SCOPE,
        content=f"turn {turn}",
        source={"session_id": session, "turn": turn, "timestamp": START.isoformat()},
        created_at=START + timedelta(minutes=turn),
    )


def memory(observation_ids: list[str], content: str) -> Memory:
    return Memory(
        scope=SCOPE,
        kind="fact",
        content=content,
        source_observation_ids=observation_ids,
        model_id="local/test-model",
        prompt_version="prompt-v1",
        confidence=0.9,
    )


class StubRepository:
    """Only the three reads the fill needs, all scope-bound like the real repository."""

    def __init__(self, observations: list[Observation], memories: dict[str, Memory]) -> None:
        self.observations = {item.id: item for item in observations}
        self.order = observations
        self.memories = memories

    def get_observation(self, scope: Scope, observation_id: str) -> Observation:
        assert scope == SCOPE, "read outside the caller's scope"
        return self.observations[observation_id]

    def list_session_observations(self, scope: Scope, session_ids: list[str]) -> list[Observation]:
        assert scope == SCOPE, "read outside the caller's scope"
        return [item for item in self.order if item.source.get("session_id") in session_ids]

    def list_memories_for_observations(self, scope: Scope, observation_ids: list[str]) -> list[Memory]:
        assert scope == SCOPE, "read outside the caller's scope"
        wanted = set(observation_ids)
        return [item for item in self.memories.values() if wanted & set(item.source_observation_ids)]


def bundle() -> tuple[StubRepository, RetrievalResult]:
    turns = [observation(turn) for turn in range(1, 7)]
    memories = {f"m{turn}": memory([turns[turn - 1].id], f"turn {turn} text") for turn in range(1, 7)}
    shown = memories["m3"]
    seed = RetrievalResult(
        memory=shown,
        source_observation_ids=list(shown.source_observation_ids),
        lexical_score=1.0,
        vector_score=1.0,
        fused_score=1.0,
    )
    return StubRepository(turns, memories), seed


def test_switch_off_by_default() -> None:
    assert fill_budget_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", "TRUE", " On "])
def test_switch_reads_the_same_values_as_the_other_switches(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_FILL_BUDGET", value)
    assert fill_budget_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "maybe"])
def test_switch_ignores_everything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_FILL_BUDGET", value)
    assert fill_budget_enabled() is False


def test_window_and_seeds_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_FILL_WINDOW", raising=False)
    monkeypatch.delenv("SEVERIAN_FILL_SEEDS", raising=False)
    assert neighbour_window() == FILL_WINDOW == 2
    assert fill_seeds() == FILL_SEEDS == 3
    monkeypatch.setenv("SEVERIAN_FILL_WINDOW", "99")
    monkeypatch.setenv("SEVERIAN_FILL_SEEDS", "0")
    assert neighbour_window() <= 5, "a huge window would read the whole session"
    assert fill_seeds() >= 1


def test_the_turns_either_side_come_first_nearest_first() -> None:
    repository, seed = bundle()
    found = neighbour_memories(repository, SCOPE, [seed], window=2, seeds=3)
    assert [item.content for item in found] == ["turn 1 text", "turn 2 text", "turn 4 text", "turn 5 text"], (
        "neighbours come from both sides, nearest turn first, and never include the item already shown"
    )


def test_a_shown_item_is_never_returned_twice() -> None:
    repository, seed = bundle()
    other = repository.memories["m4"]
    second = RetrievalResult(
        memory=other,
        source_observation_ids=list(other.source_observation_ids),
        lexical_score=1.0,
        vector_score=1.0,
        fused_score=1.0,
    )
    found = neighbour_memories(repository, SCOPE, [seed, second], window=2, seeds=3)
    contents = [item.content for item in found]
    assert len(contents) == len(set(contents)), "no duplicates when two seeds share a neighbour"
    assert "turn 3 text" not in contents and "turn 4 text" not in contents, (
        "an item already shown is not appended again"
    )


def test_only_the_top_seeds_are_expanded() -> None:
    repository, seed = bundle()
    one = neighbour_memories(repository, SCOPE, [seed], window=1, seeds=1)
    assert sorted(item.content for item in one) == ["turn 2 text", "turn 4 text"], "one seed, one turn each side"


def test_a_seed_with_no_session_adds_nothing() -> None:
    repository, seed = bundle()
    for item in repository.observations.values():
        item.source.pop("session_id", None)
    assert neighbour_memories(repository, SCOPE, [seed], window=2, seeds=3) == []


def test_conversation_order_is_not_the_repository_order() -> None:
    """The repository returns a session's turns in id order; the fill must place seeds by turn number.

    A fixed, non-monotonic permutation: with the turn-number sort removed, turn 3 sits at index 3 of this list and
    its "neighbours" become turns 6 and 2, so the assertion below fails. Reversal would not catch that.
    """
    repository, seed = bundle()
    order = [3, 0, 5, 2, 1, 4]  # turns 4, 1, 6, 3, 2, 5
    repository.order = [repository.order[index] for index in order]
    found = neighbour_memories(repository, SCOPE, [seed], window=1, seeds=1)
    assert sorted(item.content for item in found) == ["turn 2 text", "turn 4 text"], (
        "the turns either side of turn 3, whatever order the repository hands them back in"
    )


def test_a_closed_neighbour_is_dropped_when_the_caller_says_so() -> None:
    """Truth resolution marks a replaced fact with ``valid_until``; the now view hides it and the fill must too."""
    repository, seed = bundle()
    closed = repository.memories["m2"]
    closed.valid_until = START
    closed.replaced_by_id = repository.memories["m5"].id
    with_closed = neighbour_memories(repository, SCOPE, [seed], window=1, seeds=1)
    assert "turn 2 text" in [item.content for item in with_closed], (
        "with truth resolution off the closed fact is still shown, exactly as search does"
    )
    dropped = neighbour_memories(repository, SCOPE, [seed], window=1, seeds=1, drop_closed=True)
    assert [item.content for item in dropped] == ["turn 4 text"], "with it on, the closed neighbour is not appended"
