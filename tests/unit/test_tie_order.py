"""Fix (d), SEVERIAN_TIE_ORDER: when returned items share a time label, say which was said first.

Round 3 (BEAM knowledge update): BEAM stamps every message of a chat with the same date and 09:00, so the old and the
new value ("deadline April 1" / "new deadline April 5") looked equally recent; 2 of Severian's 3 misses. Labels
that two or more returned items share get the message order ("[2024/03/15 (Fri) 09:00, 2nd at this time]").
"""
from __future__ import annotations

import pytest

from severian.application.tieorder import mark_ties, ordinal, tie_order_enabled

L = "[2024/03/15 (Fri) 09:00]"
OTHER = "[2024/04/10 (Wed) 09:00] user: Sprint 2 starts on Monday."


def marked(nth: str) -> str:
    return f"[2024/03/15 (Fri) 09:00, {nth} at this time]"


def test_items_that_share_a_time_label_get_their_message_order() -> None:
    contents = [f"{L} user: The sprint 1 deadline is April 1.", f"{L} user: The new deadline is April 5.", OTHER]
    assert mark_ties(contents, [(1,), (2,), (3,)]) == [
        f"{marked('1st')} user: The sprint 1 deadline is April 1.",
        f"{marked('2nd')} user: The new deadline is April 5.",
        OTHER,
    ]


def test_the_order_follows_the_messages_not_the_ranking() -> None:
    contents = [f"{L} newer", f"{L} older"]
    assert mark_ties(contents, [(5,), (2,)]) == [f"{marked('2nd')} newer", f"{marked('1st')} older"]


def test_items_from_the_same_message_share_one_place() -> None:
    contents = [f"{L} a fact", f"{L} the turn it came from", f"{L} a later turn"]
    assert mark_ties(contents, [(1,), (1,), (2,)]) == [
        f"{marked('1st')} a fact",
        f"{marked('1st')} the turn it came from",
        f"{marked('2nd')} a later turn",
    ]


def test_an_item_with_unknown_order_is_left_as_it_was() -> None:
    contents = [f"{L} one", f"{L} two", f"{L} three"]
    assert mark_ties(contents, [(1,), None, (2,)]) == [f"{marked('1st')} one", f"{L} two", f"{marked('2nd')} three"]


@pytest.mark.parametrize(
    ("contents", "order"),
    [
        # Different labels, no label, one message: nothing to tell apart.
        ([f"{L} one", OTHER], [(1,), (2,)]),
        (["The user moved to Leeds.", "The user moved to York."], [(1,), (2,)]),
        ([f"{L} a fact", f"{L} the turn it came from"], [(1,), (1,)]),
        # A label that is not a time.
        (["[TODO] buy milk", "[TODO] call the bank"], [(1,), (2,)]),
        ([], []),
    ],
)
def test_nothing_changes_without_a_tie_to_break(contents: list[str], order: list[tuple[int, ...] | None]) -> None:
    assert mark_ties(contents, order) == contents


def test_order_and_contents_must_line_up() -> None:
    with pytest.raises(ValueError):
        mark_ties([f"{L} one"], [])


@pytest.mark.parametrize(
    ("n", "text"),
    [(1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"),
     (22, "22nd"), (23, "23rd"), (101, "101st"), (111, "111th"), (112, "112th")],
)
def test_ordinals(n: int, text: str) -> None:
    assert ordinal(n) == text


def test_the_switch_is_off_unless_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_TIE_ORDER", raising=False)
    assert not tie_order_enabled()
    monkeypatch.setenv("SEVERIAN_TIE_ORDER", "true")
    assert tie_order_enabled()
