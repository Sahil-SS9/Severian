"""The deterministic classifier behind every dreaming decision (no model, no I/O)."""

from __future__ import annotations

import pytest

from severian.application.dreamtriage import (
    COMPATIBLE,
    CONTRADICT,
    DUPLICATE,
    RESTATE,
    Triage,
    classify_pair,
    proper_tokens,
    value_bearing,
)


def classify(a: str, b: str) -> str | None:
    """Classify two texts the way the dreaming pass does (same tokenisation, same defaults)."""
    from severian.application.dreaming import triage
    from severian.domain.models import Memory, Scope

    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    memories = [
        Memory(
            scope=scope,
            kind="fact",
            content=text,
            source_observation_ids=[f"obs-{index}"],
            model_id="t",
            prompt_version="p",
            confidence=0.9,
        )
        for index, text in enumerate((a, b), start=1)
    ]
    verdicts = triage(memories)
    return verdicts.verdicts[0].kind if verdicts.verdicts else None


def test_a_restatement_of_the_same_words_is_a_duplicate() -> None:
    text = "the user keeps a steel kettle"
    assert classify(text, text) == DUPLICATE


def test_a_different_word_for_the_same_thing_is_a_restatement() -> None:
    assert classify("the user's dog is called Max", "the user's dog is named Max") == RESTATE


def test_a_different_number_is_a_contradiction() -> None:
    assert classify("the user has 2 cats", "the user has 3 cats") == CONTRADICT


def test_a_different_name_is_a_contradiction() -> None:
    assert classify("the user's flat is in Leeds", "the user's flat is in Bradford") == CONTRADICT


def test_a_negation_is_a_contradiction() -> None:
    assert classify("the user has a garden", "the user has not a garden") == CONTRADICT


def test_unrelated_memories_are_not_classified() -> None:
    assert classify("the user keeps a steel kettle", "quarterly revenue rose sharply") is None


def test_capitalised_tokens_are_found_and_only_those() -> None:
    assert proper_tokens("the user's flat is in Leeds, near the river Aire") == {"leeds", "aire"}
    assert proper_tokens("the user's flat is in a city") == set()


@pytest.mark.parametrize("token", ["2024", "3", "not", "never", "Leeds"])
def test_value_bearing_tokens(token: str) -> None:
    assert value_bearing(token, proper={"Leeds"})


@pytest.mark.parametrize("token", ["called", "named", "shed", "blue"])
def test_soft_tokens(token: str) -> None:
    assert not value_bearing(token, proper={"Leeds"})


def test_classify_pair_bands() -> None:
    assert (
        classify_pair(overlap=0.95, differing={"x"}, proper=set(), duplicate_jaccard=0.8, contradiction_jaccard=0.6)
        == DUPLICATE
    )
    assert (
        classify_pair(overlap=0.5, differing={"x"}, proper=set(), duplicate_jaccard=0.8, contradiction_jaccard=0.6)
        is None
    )
    assert (
        classify_pair(overlap=0.7, differing={"7"}, proper=set(), duplicate_jaccard=0.8, contradiction_jaccard=0.6)
        == CONTRADICT
    )
    assert (
        classify_pair(overlap=0.7, differing={"named"}, proper=set(), duplicate_jaccard=0.8, contradiction_jaccard=0.6)
        == RESTATE
    )


def test_triage_counts_are_per_class() -> None:
    from severian.application.dreamtriage import PairVerdict

    triage = Triage(
        verdicts=[
            PairVerdict("a", "b", DUPLICATE, 0.9),
            PairVerdict("c", "d", CONTRADICT, 0.7),
            PairVerdict("e", "f", RESTATE, 0.65),
            PairVerdict("g", "h", COMPATIBLE, 0.7),
        ]
    )
    assert triage.counts() == {DUPLICATE: 1, CONTRADICT: 1, RESTATE: 1, COMPATIBLE: 1}
    assert [verdict.memory_a_id for verdict in triage.duplicates] == ["a"]
