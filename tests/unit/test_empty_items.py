"""Fix (c), SEVERIAN_DROP_EMPTY: notes and turns that carry no information give their place to the next result.

Round 3 (LoCoMo multi-hop): a quarter of Severian's 10 slots held nothing ("The user is Melanie.", "The user asked
Caroline about ...", "Cool! What type of music do you play?"). They move behind every other result, so they are only
returned when nothing else is left; their order among themselves is kept.
"""
from __future__ import annotations

import pytest

from severian.application.emptyitems import demote_empty, drop_empty_enabled, is_empty

LABEL = "[2023/08/25 (Fri) 13:33] "


@pytest.mark.parametrize(
    "content",
    [
        "The user is Melanie.",
        f"{LABEL}The user is named Caroline.",
        "The user's name is Jon.",
        f"{LABEL}The user asked Caroline about her transition and joining the transgender community.",
        "The user is asking if the AI has any questions.",
        f"{LABEL}Melanie: Cool! What type of music do you play?",
        f"{LABEL}Caroline: Thanks, Melanie! That's really sweet. Is this your own painting?",
        "user: Can you check the train times?",
    ],
)
def test_items_that_say_nothing_are_empty(content: str) -> None:
    assert is_empty(content, "What instruments does Melanie play?")


@pytest.mark.parametrize(
    "content",
    [
        # Facts, even short ones.
        "The user is vegetarian.",
        "The user is Melanie's friend from the support group.",
        "The user Melanie went on another camping trip in the forest.",
        "The user wants to know how to fix the bike chain.",
        # A question with a statement in it carries information.
        f"{LABEL}Melanie: Wow, Melanie! I'm getting creative too, just learning the piano. What made you try pottery?",
        f"{LABEL}Caroline: Nope, never been to something like that. What was it about?",
        # A question followed by the assistant's reply: the reply is the information.
        "user: What time is the Lisbon flight?\nassistant: It leaves at 06:40 from Gatwick.",
        # A long question is not a bare question.
        "user: " + "Could you remind me which of the three hotels near the station had the late check-in? " * 2,
        # Statements.
        f"{LABEL}Gina: I lost my job as a banker yesterday.",
    ],
)
def test_items_with_information_are_not_empty(content: str) -> None:
    assert not is_empty(content, "What happened to Gina?")


def test_a_name_question_keeps_name_notes() -> None:
    assert not is_empty("The user is Melanie.", "What is my name?")
    assert not is_empty("The user is named Caroline.", "Who is the user?")


def test_a_question_about_what_was_asked_keeps_asked_notes_and_questions() -> None:
    query = "What did Melanie ask Caroline about the painting?"
    assert not is_empty(f"{LABEL}The user asked Caroline to explain why the rainbow flag mural is special.", query)
    assert not is_empty(f"{LABEL}Caroline: Thanks, Melanie! Is this your own painting?", query)


def test_empty_items_move_behind_every_other_result_in_their_own_order() -> None:
    ranked = [
        "The user is Melanie.",
        "Melanie plays the clarinet.",
        "Melanie: Cool! What type of music do you play?",
        "Caroline: I play the violin in a quartet.",
        "The user asked Caroline about her music.",
    ]
    assert demote_empty(ranked, content=lambda item: item, query="What instruments do they play?") == [
        "Melanie plays the clarinet.",
        "Caroline: I play the violin in a quartet.",
        "The user is Melanie.",
        "Melanie: Cool! What type of music do you play?",
        "The user asked Caroline about her music.",
    ]


def test_nothing_moves_when_nothing_is_empty() -> None:
    ranked = ["Melanie plays the clarinet.", "Caroline plays the violin."]
    assert demote_empty(ranked, content=lambda item: item, query="Who plays what?") == ranked


def test_the_switch_is_off_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_DROP_EMPTY", raising=False)
    assert not drop_empty_enabled()
    monkeypatch.setenv("SEVERIAN_DROP_EMPTY", "1")
    assert drop_empty_enabled()
