"""Fix (b), SEVERIAN_CONFLICT_NOTES: say so when two returned memories disagree.

Round 3 (BEAM contradiction resolution): Severian returned both sides in all 10 questions ("The user has not
completed any coin toss problems before." and "The user has already completed 5 coin toss problems") but the
answer mentioned the conflict in only 2. The note names the two statements so the answer can raise it.
"""
from __future__ import annotations

import pytest

from severian.application.conflicts import conflict_note, conflict_notes_enabled

LABEL = "[2024/03/15 (Fri) 09:00] "
ASK = "If it matters, mention both."  # not "and ask which is right": the answer hedged instead of adding up (21/10)


def test_a_denial_and_a_matching_statement_get_a_note() -> None:
    note = conflict_note(
        [
            "The user has not completed any coin toss problems before.",
            "The user likes probability puzzles.",
            "The user has already completed 5 coin toss problems with 80% mastery.",
        ]
    )
    assert note == (
        'Memories that disagree: "The user has not completed any coin toss problems before" but also '
        f'"The user has already completed 5 coin toss problems with 80% mastery". {ASK}'
    )


def test_turns_count_only_what_the_user_said_in_the_denying_clause() -> None:
    note = conflict_note(
        [
            f"{LABEL}user: I've never fixed any bugs related to the autocomplete feature, but I want to make sure "
            "I'm handling errors well.\nassistant: Sure. You have fixed nothing yet, so start with tests.",
            f"{LABEL}user: I fixed two autocomplete bugs last week by adding null checks.\nassistant: Nice work.",
        ]
    )
    assert note is not None
    assert "\"I've never fixed any bugs related to the autocomplete feature\"" in note
    assert '"I fixed two autocomplete bugs last week by adding null checks"' in note


@pytest.mark.parametrize(
    "contents",
    [
        # One shared topic word is not enough.
        ["user: I've never been to Japan.\nassistant: ok", "user: I'd love to visit Japan next spring.\nassistant: ok"],
        # Both sides deny.
        ["The user has never obtained an API key.", "The user does not have an API key yet."],
        # A question is not a claim.
        ["user: Have I never finished the coin toss problems?", "The user finished the coin toss problems."],
        # Different people in one chat.
        ["Caroline: I've never painted a mural before.", "Melanie: I painted a mural with my kids last summer."],
        # The assistant's reply is not the user's claim.
        [
            "user: I am learning Bootstrap.\nassistant: You have never used Bootstrap modal components before.",
            "The user uses Bootstrap modal components for the project popups.",
        ],
        # Nothing to compare.
        ["The user has never attempted triangle classification problems."],
        [],
    ],
)
def test_no_note_without_a_real_disagreement(contents: list[str]) -> None:
    assert conflict_note(contents) is None


def test_a_denial_and_its_match_in_the_same_memory_are_not_a_conflict() -> None:
    assert conflict_note(["user: I've never done coin toss problems. I did 5 coin toss problems today."]) is None


def test_the_note_names_at_most_two_disagreements() -> None:
    contents = [
        "The user has never attempted triangle classification problems.",
        "The user completed 10 triangle classification problems.",
        "The user has not completed any coin toss problems before.",
        "The user completed 5 coin toss problems.",
        "The user has never studied conditional probability problems.",
        "The user solved 12 conditional probability problems.",
    ]
    note = conflict_note(contents)
    assert note is not None
    assert note.count(" but also ") == 2
    assert "triangle" in note and "coin toss" in note and "conditional" not in note


def test_a_plain_claim_is_quoted_before_a_stated_goal() -> None:
    note = conflict_note(
        [
            "The user has never completed any problems involving medians or altitudes before.",
            "The user wants to solve problems involving triangle area formulas, medians and altitudes.",
            "The user recently solved 8 problems on medians and altitudes.",
        ]
    )
    assert note is not None
    assert '"The user recently solved 8 problems on medians and altitudes"' in note
    assert "wants to" not in note


def test_with_a_question_the_note_appears_only_when_the_disagreement_is_about_it() -> None:
    contents = ["The user has not completed any coin toss problems before.", "The user completed 5 coin toss problems."]
    assert conflict_note(contents, query="Have I completed any coin toss problems before?") is not None
    assert conflict_note(contents, query="Have I done coin tosses?") is not None
    assert conflict_note(contents, query="What feedback did I get on the dependent events quiz?") is None
    # A one-topic question needs only that topic.
    api = ["The user has never obtained an API key.", "user: I'm worried about exposing my API key in the repo."]
    assert conflict_note(api, query="Do I have an API key?") is not None


def test_a_restated_denial_is_named_once() -> None:
    note = conflict_note(
        [
            "The user has not completed any coin toss problems before.",
            "user: I've never completed any coin toss problems before.\nassistant: Let's start.",
            "The user has already completed 5 coin toss problems with 80% mastery.",
        ]
    )
    assert note is not None
    assert note.count(" but also ") == 1


def test_a_goal_counts_only_when_it_covers_everything_the_denial_is_about() -> None:
    denial = "user: I've never fixed any bugs related to the autocomplete feature.\nassistant: ok"
    partial = "user: I'm trying to finish the city autocomplete feature by Friday.\nassistant: ok"
    full = "user: I'm trying to fix the remaining autocomplete feature bugs today.\nassistant: ok"
    assert conflict_note([denial, partial]) is None
    assert conflict_note([denial, full]) is not None


def test_a_long_denial_is_quoted_around_the_denial() -> None:
    start = "The user is having trouble applying the triangle classification methods to many real-world problems"
    note = conflict_note(
        [
            f"{start} in their homework and their weekly quizzes, especially since they have never worked on "
            "triangle classification problems before.",
            "The user completed 10 triangle classification problems with 8 correct.",
        ]
    )
    assert note is not None
    quoted = note.split('"')[1]
    assert "never worked on triangle classification problems" in quoted
    assert len(quoted) <= 152


def test_long_statements_are_shortened_at_a_word() -> None:
    long_tail = " ".join(["with", "careful", "notes"] * 40)
    note = conflict_note(
        [
            f"The user has never completed any coin toss problems {long_tail}.",
            "The user completed 5 coin toss problems.",
        ]
    )
    assert note is not None
    quoted = note.split('"')[1]
    assert len(quoted) <= 151 and quoted.endswith("…") and not quoted[:-1].endswith(" ")


def test_the_switch_is_on_unless_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_CONFLICT_NOTES", raising=False)
    assert conflict_notes_enabled()
    for off in ("0", "false", "OFF", " no "):
        monkeypatch.setenv("SEVERIAN_CONFLICT_NOTES", off)
        assert not conflict_notes_enabled()
    monkeypatch.setenv("SEVERIAN_CONFLICT_NOTES", "1")
    assert conflict_notes_enabled()
