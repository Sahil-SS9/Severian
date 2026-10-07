"""Round 2 improvement 3, preference card: the user's stated preferences, returned alongside every search.

Built by rules from user facts (no model call), behind ``SEVERIAN_PREFERENCE_CARD`` (off = no card, search
unchanged). The card sits outside the search results, has a hard cap of about 500 tokens and never grows:
newest preferences first, older ones drop off. Rebuilt only when the scope's memories change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from severian.application.prefcard import CARD_TOKEN_CAP, build_card, is_preference, preference_card_enabled
from severian.application.userfacts import Extraction
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

SCOPE = Scope(tenant_id="kensei", profile_id="sahil", collection_id="profile", agent_id="kensei", session_id="s1")


def test_the_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("SEVERIAN_PREFERENCE_CARD", raising=False)
    assert not preference_card_enabled()
    monkeypatch.setenv("SEVERIAN_PREFERENCE_CARD", "1")
    assert preference_card_enabled()


@pytest.mark.parametrize(
    "fact",
    [
        "The user prefers PC as a platform.",
        "The user likes spicy food.",
        "The user loves hiking in the Alps.",
        "The user enjoys baking sourdough.",
        "The user hates early meetings.",
        "The user dislikes coriander.",
        "The user avoids caffeine after noon.",
        "The user is vegetarian.",
        "The user is allergic to penicillin.",
        "The user doesn't eat pork.",
        "The user's favourite band is Radiohead.",
        "[2023/04/27 (Thu) 20:30] The user prefers aisle seats.",
    ],
)
def test_stated_preferences_are_recognised(fact):
    assert is_preference(fact)


@pytest.mark.parametrize(
    "fact",
    [
        "The user works at Rightmove.",
        "The user wants to track their expenses.",  # a goal, not a preference
        "The user wants to be reminded to run tests before pushing.",  # an instruction for later
        "The gift card was from the sister's favorite clothing store.",  # about someone else
        "The user is interested in the AI-powered CRM trend.",
    ],
)
def test_other_facts_are_not_preferences(fact):
    assert not is_preference(fact)


def test_card_lists_preferences_newest_first_without_repeats():
    card = build_card(
        ["The user likes tea.", "The user works at Rightmove.", "The user hates rain.", "The user likes tea."]
    )
    # the last "likes tea" is the newest mention, so it leads; the older repeat is dropped
    assert card == "The user's stated preferences (newest first):\n- The user likes tea.\n- The user hates rain."


def test_no_preferences_means_no_card():
    assert build_card(["The user works at Rightmove."]) is None


def test_the_card_never_grows_past_the_cap_and_keeps_the_newest():
    facts = [f"The user likes thing number {n} very much indeed." for n in range(400)]
    card = build_card(facts)
    assert card is not None
    assert len(card.split()) * 4 // 3 <= CARD_TOKEN_CAP
    assert "number 399 " in card and "number 0 " not in card


# -- wiring ---------------------------------------------------------------------------------------------


class Facts:
    model = "fake-model"

    def __init__(self) -> None:
        self.reply = ["The user prefers aisle seats.", "The user works at Rightmove."]

    def extract(self, messages: list[str]) -> Extraction:
        return Extraction(facts={i: list(self.reply) for i, _ in enumerate(messages, 1)})


def build(tmp_path: Path, monkeypatch, *, on: bool) -> tuple[ServiceBundle, Facts]:
    monkeypatch.setenv("SEVERIAN_USER_FACTS", "1")
    if on:
        monkeypatch.setenv("SEVERIAN_PREFERENCE_CARD", "1")
    else:
        monkeypatch.delenv("SEVERIAN_PREFERENCE_CARD", raising=False)
    tmp_path.mkdir(parents=True, exist_ok=True)
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    facts = Facts()
    built.derivation._fact_extractor = facts
    return built, facts


def say(bundle: ServiceBundle, text: str, turn: int) -> None:
    bundle.service.ingest_observation(
        SCOPE, content=f"user: {text}\nassistant: ok", source={"session_id": "s1", "turn": turn}
    )
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).failed == 0
    assert worker.extract_facts().failed == 0


def test_with_the_switch_on_the_service_returns_the_card(tmp_path, monkeypatch):
    bundle, _ = build(tmp_path, monkeypatch, on=True)
    try:
        assert bundle.service.preference_card(SCOPE) is None
        say(bundle, "I always take an aisle seat.", 1)
        assert bundle.service.preference_card(SCOPE) == (
            "The user's stated preferences (newest first):\n- The user prefers aisle seats."
        )
    finally:
        bundle.close()


def test_the_card_follows_new_facts(tmp_path, monkeypatch):
    bundle, facts = build(tmp_path, monkeypatch, on=True)
    try:
        say(bundle, "Aisle please.", 1)
        assert "aisle" in (bundle.service.preference_card(SCOPE) or "")
        facts.reply = ["The user dislikes coriander."]
        say(bundle, "No coriander for me.", 2)
        card = bundle.service.preference_card(SCOPE) or ""
        assert card.index("coriander") < card.index("aisle")
    finally:
        bundle.close()


def test_with_the_switch_off_there_is_no_card_and_search_is_unchanged(tmp_path, monkeypatch):
    bundle, _ = build(tmp_path, monkeypatch, on=False)
    try:
        say(bundle, "I always take an aisle seat.", 1)
        assert bundle.service.preference_card(SCOPE) is None
        assert all("stated preferences" not in hit.content for hit in bundle.service.search(SCOPE, query="seat"))
    finally:
        bundle.close()
