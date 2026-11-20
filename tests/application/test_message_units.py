"""S1 message units: role split, order, context ordering and selection."""
from __future__ import annotations

import os

from severian.application.message_units import (
    MESSAGE_UNIT_KIND,
    mark_later_units,
    message_unit_memories,
    order_unit_hits,
    prefer_message_units,
)
from severian.application.retrieval import RetrievalResult
from severian.application.service import SearchHitView
from severian.domain.models import Memory, Observation, Scope


def _scope() -> Scope:
    return Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


def _observation() -> Observation:
    return Observation(
        scope=_scope(),
        content="[2024/03/15 (Fri)] user: the deadline is April 1\nassistant: noted, April 1",
        source={"turn": 3, "session_id": "s0001"},
    )


def test_units_split_roles_and_carry_turn_order(monkeypatch) -> None:
    monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
    units = message_unit_memories(_observation())
    assert [unit.kind for unit in units] == [MESSAGE_UNIT_KIND, MESSAGE_UNIT_KIND]
    assert [unit.content for unit in units] == [
        "[2024/03/15 (Fri)] user message 6: the deadline is April 1",
        "[2024/03/15 (Fri)] assistant message 7: noted, April 1",
    ]
    assert all(unit.source_observation_ids for unit in units)


def test_long_messages_chunk_on_sentence_boundaries(monkeypatch) -> None:
    monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
    text = " ".join(f"Sentence {number} is here." for number in range(200))
    observation = Observation(
        scope=_scope(),
        content=f"[2024/03/15 (Fri)] user: {text}",
        source={"turn": 1},
    )
    units = message_unit_memories(observation)
    assert len(units) > 1
    for unit in units:
        body = unit.content.split(": ", 1)[1]
        assert body.endswith(".")


def test_same_date_units_are_ordered_and_marked_later() -> None:
    early = SearchHitView(
        memory_id="early",
        content="[2024/03/15 (Fri)] user message 6: deadline April 1",
        source_observation_ids=["o1"],
        lexical_score=1.0,
        vector_score=1.0,
        fused_score=1.0,
        scope=_scope(),
    )
    late = SearchHitView(
        memory_id="late",
        content="[2024/03/15 (Fri)] user message 26: deadline April 5",
        source_observation_ids=["o2"],
        lexical_score=1.0,
        vector_score=1.0,
        fused_score=1.0,
        scope=_scope(),
    )
    ordered = order_unit_hits([late, early])
    assert [hit.memory_id for hit in ordered] == ["early", "late"]
    labels = mark_later_units(ordered)
    assert labels["early"] == early.content
    assert labels["late"].endswith("[later message]")


def test_prefer_units_drops_the_parent_turn_but_keeps_user_facts() -> None:
    def result(memory_id: str, kind: str, source: str) -> RetrievalResult:
        return RetrievalResult(
            memory=Memory(
                id=memory_id,
                scope=_scope(),
                kind=kind,
                content=memory_id,
                source_observation_ids=[source],
                model_id="m",
                prompt_version="p",
                confidence=1.0,
            ),
            source_observation_ids=[source],
            lexical_score=1.0,
            vector_score=1.0,
            fused_score=1.0,
        )

    unit = result("unit", MESSAGE_UNIT_KIND, "o1")
    parent = result("parent", "fact", "o1")
    fact = result("fact", "user_fact", "o1")
    other = result("other", "fact", "o2")
    assert [item.memory.id for item in prefer_message_units([parent, unit, fact, other])] == ["unit", "fact", "other"]


def test_switch_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("SEVERIAN_MESSAGE_UNITS", raising=False)
    assert "SEVERIAN_MESSAGE_UNITS" not in os.environ
    assert message_unit_memories(_observation()) == []
