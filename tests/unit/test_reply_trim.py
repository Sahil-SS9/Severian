"""Fix (a), SEVERIAN_REPLY_TRIM: when the returned turns do not fit their space, the assistant's replies go first.

Round 3 trial (20/10/2026, LongMemEval, 60 questions): cutting every saved turn down to what the user said took
the 1,500-character score from 43% to 68% but cost points with no limit. So the cut happens only when the turns
would not fit, before any of the user's words are cut, and never when the question asks what was said.
"""
from __future__ import annotations

import pytest

from severian.application.replytrim import (
    asks_what_was_said,
    fit_contents,
    reply_trim_enabled,
    without_reply,
)

TURN = "[2024/03/15 (Fri) 09:00] user: My sprint 1 deadline is now April 5.\nassistant: Noted. Here is a long plan."
PLAIN = "user: I moved to Leeds in May.\nassistant: Leeds is lovely. Here are ten things to do there."
FACT = "The user moved to Leeds in May."


def test_a_saved_turn_keeps_its_time_label_and_the_users_words() -> None:
    assert without_reply(TURN) == "[2024/03/15 (Fri) 09:00] user: My sprint 1 deadline is now April 5."
    assert without_reply(PLAIN) == "user: I moved to Leeds in May."


@pytest.mark.parametrize(
    "content",
    [
        FACT,
        "user: a message with no reply",
        "[2024/03/15 (Fri) 09:00] assistant text saved on its own",
        "Caroline: I went to the support group yesterday.\nassistant: not a user turn",
    ],
)
def test_anything_that_is_not_a_saved_user_turn_is_left_whole(content: str) -> None:
    assert without_reply(content) == content


def test_when_everything_fits_nothing_changes() -> None:
    contents = [TURN, PLAIN, FACT]
    assert fit_contents(contents, sum(map(len, contents))) == contents


def test_when_nothing_less_will_do_every_reply_goes_before_any_user_words() -> None:
    contents = [TURN, PLAIN, FACT]
    assert fit_contents(contents, 1) == [without_reply(TURN), without_reply(PLAIN), FACT]


def test_replies_go_from_the_lowest_ranked_turn_up_and_stop_once_everything_fits() -> None:
    # Round 3 re-run (21/10/2026): cutting every reply as soon as anything did not fit left ~74% of the 8,000
    # characters empty on LongMemEval, and 3 lost answers were inside a reply that would have fitted.
    contents = [TURN, PLAIN, FACT]
    total = sum(map(len, contents))
    plain_reply = len(PLAIN) - len(without_reply(PLAIN))
    assert fit_contents(contents, total - 1) == [TURN, without_reply(PLAIN), FACT]
    assert fit_contents(contents, total - plain_reply) == [TURN, without_reply(PLAIN), FACT]
    assert fit_contents(contents, total - plain_reply - 1) == [without_reply(TURN), without_reply(PLAIN), FACT]


def test_facts_are_skipped_when_choosing_which_reply_goes_next() -> None:
    contents = [TURN, FACT, PLAIN, FACT]
    assert fit_contents(contents, sum(map(len, contents)) - 1) == [TURN, FACT, without_reply(PLAIN), FACT]


def test_the_callers_measure_decides_when_it_fits() -> None:
    contents = [TURN, PLAIN]
    # A measure that charges 100 per item on top of the text: cutting PLAIN alone is not enough.
    measure = lambda items: sum(len(item) + 100 for item in items)  # noqa: E731
    budget = measure(contents) - (len(PLAIN) - len(without_reply(PLAIN))) - 1
    assert fit_contents(contents, budget, cost=measure) == [without_reply(TURN), without_reply(PLAIN)]


@pytest.mark.parametrize(
    "question",
    [
        "Can you remind me what you said about Leeds?",
        "How did you recommend structuring the work?",
        "What did you suggest for the sprint plan?",
        "In our previous conversation, which hotel came first?",
        "What did we discuss about the deadline?",
    ],
)
def test_a_question_about_what_was_said_keeps_the_replies(question: str) -> None:
    assert asks_what_was_said(question)
    assert fit_contents([TURN, PLAIN], 10, query=question) == [TURN, PLAIN]


@pytest.mark.parametrize("question", ["When is my sprint 1 deadline?", "Where did I move to?", "", None])
def test_ordinary_questions_do_not_ask_what_was_said(question: str | None) -> None:
    assert not asks_what_was_said(question)


def test_the_switch_is_off_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_REPLY_TRIM", raising=False)
    assert not reply_trim_enabled()
    for value in ("1", "true", "on", "yes"):
        monkeypatch.setenv("SEVERIAN_REPLY_TRIM", value)
        assert reply_trim_enabled()
    monkeypatch.setenv("SEVERIAN_REPLY_TRIM", "0")
    assert not reply_trim_enabled()
