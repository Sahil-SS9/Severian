"""SEVERIAN_SUFFICIENCY: one line when the returned items do not hold the detail the question asks for.

The answer model fills a gap from nearby text: BEAM abstention questions are answered with a guess 40% of the time.
The note is a warning to the reader, so it has to be precise: firing on a question the items do answer costs a right
answer. The rule is therefore lexical and narrow - it fires only when the question names a detail slot from a short
curated list (criteria, rationale, agenda, reaction, terms, rules, ...) and no returned item mentions it, or when the
question asks for a specific/exact thing and the words right after that marker are absent. Both are measured against
controls in the handoff evidence; see tests/application/test_context_efficiency.py for the end-to-end path.
"""
from __future__ import annotations

import pytest

from severian.application.sufficiency import (
    NOTE,
    sufficiency_enabled,
    sufficiency_note,
)

TRELLO = [
    "[2024/03/18 (Mon) 09:00] user: I set up the Trello board for sprint 1 and moved every card across.\n"
    "assistant: Good. Keep the board moving and check it daily.",
    "[2024/03/20 (Wed) 10:00] user: I spent the morning prioritizing tasks on the Trello board.\n"
    "assistant: Prioritizing early keeps sprint 1 on track.",
]


def test_switch_off_by_default() -> None:
    assert sufficiency_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "on", "yes"])
def test_switch_on_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_SUFFICIENCY", value)
    assert sufficiency_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
def test_switch_off_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_SUFFICIENCY", value)
    assert sufficiency_enabled() is False


def test_the_note_is_one_line() -> None:
    assert "\n" not in NOTE
    assert len(NOTE) <= 130, "the note is paid for out of the same character budget as the memories"
    assert "may not be in memory" in NOTE


@pytest.mark.parametrize(
    ("query", "items"),
    [
        ("What specific criteria did I use to prioritize tasks on the Trello board during sprint 1?", TRELLO),
        ("What was the rationale behind choosing Formspree API v2 for contact form submission?", TRELLO),
        ("Can you share the agenda or structure of the sessions where I practiced notation?", TRELLO),
        ("What was my emotional reaction to confusing mutually exclusive and independent events?", TRELLO),
        ("What exact discount did the supplier give me?", TRELLO),
    ],
)
def test_fires_when_the_asked_detail_is_nowhere_in_the_items(query: str, items: list[str]) -> None:
    assert sufficiency_note(query, items) == NOTE


@pytest.mark.parametrize(
    ("query", "items"),
    [
        # the slot word is in the items, so the detail may well be there
        (
            "What specific criteria did I use to prioritize tasks on Trello?",
            TRELLO + ["user: My criteria were impact and effort."],
        ),
        (
            "What was the rationale behind choosing Formspree API v2?",
            TRELLO + ["user: The rationale was its free tier."],
        ),
        # an idiom, not a detail slot
        (
            "How much is the painting worth in terms of the amount I paid for it?",
            TRELLO + ["user: It is worth triple."],
        ),
        # nothing returned: there is no block to warn about
        ("What specific criteria did I use?", []),
        # no detail slot and no explicit marker
        ("What is Jon's favorite style of dance?", TRELLO),
        ("Remind me what you said about the sprint 1 board.", TRELLO),
        # a summary request, not a detail lookup
        ("Summarise everything I said about Trello in sprint 1.", TRELLO),
    ],
)
def test_never_fires_when_the_detail_may_be_there(query: str, items: list[str]) -> None:
    assert sufficiency_note(query, items) is None


def test_matches_singular_and_plural_of_a_slot_word() -> None:
    assert sufficiency_note("What specific rule did I enforce on Airbnb lint?", TRELLO) == NOTE
    assert sufficiency_note("What specific rules did I enforce on Airbnb lint?", TRELLO) == NOTE
    assert sufficiency_note("What specific rule did I enforce?", ["user: The rule was no unused vars."]) is None


def test_a_missing_query_means_no_note() -> None:
    assert sufficiency_note(None, TRELLO) is None
    assert sufficiency_note("   ", TRELLO) is None
