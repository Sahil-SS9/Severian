"""Deterministic "newer statement wins" rule for search results (improvement 2).

Two memories are treated as the same statement with different values when
they share an opening of at least three words that ends in a linking word
("is", "at", "on", ...) covering at least half of the shorter one, and each
then goes on to say something different:

    "The deploy window is | Thursday afternoon"
    "The deploy window is | Friday morning"

Only the newer one is returned. Statements that merely share words, start
differently, or use a non-linking verb ("The user prefers dark mode" /
"The user prefers green tea" both stay) are untouched. Nothing is deleted:
the older memory stays in the store and in history; only active search
hides it. Kept deliberately narrow: a missed contradiction costs one stale
line, a false one hides a true fact.

Truth state (Step 1, ``SEVERIAN_TRUTH_STATE``): ``assess_truth`` labels every item
current / historical / transition / concurrent. An older statement is only
``historical`` (hidden from the default "now" view) when the newer one certainly
replaces it: the newer text says it changed ("now", "moved", "no longer"), or both
values are one-off and mutually exclusive. Recurring or plural values ("open
Mondays" / "open Fridays") are ``concurrent`` and both stay.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeVar

from severian.domain.models import TruthState

_WORD = re.compile(r"\w+", re.UNICODE)
_LINKING_WORDS = frozenset({"is", "are", "was", "were", "at", "on", "to", "now", "becomes", "equals"})
_MIN_SHARED_WORDS = 3

T = TypeVar("T")


def _words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def _shared_opening(words_a: list[str], words_b: list[str]) -> int | None:
    """Length of the shared opening (cut back to a linking word); None when not the same statement."""
    shared = 0
    for left, right in zip(words_a, words_b, strict=False):
        if left != right:
            break
        shared += 1
    if shared == len(words_a) or shared == len(words_b):
        return None  # one restates or extends the other: not a different value
    # Cut the shared opening back to its last linking word ("is at | 09 15").
    while shared and words_a[shared - 1] not in _LINKING_WORDS:
        shared -= 1
    shorter = min(len(words_a), len(words_b))
    return shared if shared >= _MIN_SHARED_WORDS and shared * 2 >= shorter else None


def same_statement_different_value(a: str, b: str) -> bool:
    return _shared_opening(_words(a), _words(b)) is not None


# Explicit change wording in the NEWER statement: the source itself says the old value stopped.
_CHANGE_WORDS = frozenset(
    {"now", "moved", "changed", "switched", "rescheduled", "instead", "anymore", "replaced", "relocated"}
)
_CHANGE_PHRASES = ("no longer", "not any more", "from now on", "changed to", "moved to")
# Values that can hold together with a sibling value ("open Mondays" + "open Fridays"): recurring
# or plural days/parts of day, list words and "also"-style words.
_ADDITIVE_WORDS = frozenset(
    {
        "every", "each", "also", "too", "both", "and", "plus", "additionally", "as", "well", "daily", "weekly",
        "monthly", "weekdays", "weekends", "mornings", "afternoons", "evenings", "nights", "always", "sometimes",
    }
)
_PLURAL_DAY = re.compile(r"^(mon|tues|wednes|thurs|fri|satur|sun)days$")


def has_change_wording(text: str) -> bool:
    """True when ``text`` itself says something changed (\"now\", \"moved\", \"no longer\", ...)."""
    words = _words(text)
    joined = " ".join(words)
    return any(word in _CHANGE_WORDS for word in words) or any(phrase in joined for phrase in _CHANGE_PHRASES)


def _additive(words: list[str]) -> bool:
    return any(word in _ADDITIVE_WORDS or _PLURAL_DAY.match(word) for word in words)


def replaces(older: str, newer: str) -> bool:
    """True when ``newer`` certainly replaces ``older`` (same statement, different value).

    Certain means: the newer text says it changed (\"now\", \"moved\", \"no longer\"), or the two
    values are one-off, mutually exclusive values (\"Thursday afternoon\" / \"Friday morning\").
    Recurring, plural or list-like values (\"open Mondays\" / \"open Fridays\") can both be true,
    so wording similarity alone never hides one of them.
    """
    words_older, words_newer = _words(older), _words(newer)
    shared = _shared_opening(words_older, words_newer)
    if shared is None:
        return False
    if has_change_wording(newer):
        return True
    return not (_additive(words_older[shared:]) or _additive(words_newer[shared:]))


@dataclass(frozen=True)
class TruthAssessment:
    """Truth state of one item among a result pool (``previous`` = keys it replaced)."""

    state: TruthState
    replaced_by: str | None = None
    previous: tuple[str, ...] = ()


def assess_truth(
    items: Sequence[T], *, content: Callable[[T], str], created_at: Callable[[T], datetime], key: Callable[[T], str]
) -> dict[str, TruthAssessment]:
    """Truth state of every item, deterministic from content and age; nothing is stored or deleted."""
    replaced_by: dict[str, str] = {}
    replacer_age: dict[str, tuple[datetime, str]] = {}
    previous: dict[str, list[str]] = {}
    same_head: set[str] = set()
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            if key(first) == key(second) or not same_statement_different_value(content(first), content(second)):
                continue
            same_head.update((key(first), key(second)))
            older, newer = sorted((first, second), key=lambda item: (created_at(item), key(item)))
            if replaces(content(older), content(newer)):
                age = (created_at(newer), key(newer))
                if key(older) not in replacer_age or age > replacer_age[key(older)]:
                    replacer_age[key(older)] = age  # the newest replacer is the one that counts
                    replaced_by[key(older)] = key(newer)
                previous.setdefault(key(newer), []).append(key(older))
    result: dict[str, TruthAssessment] = {}
    for item in items:
        item_key = key(item)
        if item_key in replaced_by:
            state = TruthState.HISTORICAL
        elif item_key in previous:
            state = TruthState.TRANSITION
        elif item_key in same_head:
            state = TruthState.CONCURRENT
        else:
            state = TruthState.CURRENT
        result[item_key] = TruthAssessment(state, replaced_by.get(item_key), tuple(previous.get(item_key, ())))
    return result


def drop_replaced(  # legacy rule: any same-statement pair, kept for SEVERIAN_TRUTH_STATE off
    items: Sequence[T], *, content: Callable[[T], str], created_at: Callable[[T], datetime], key: Callable[[T], str]
) -> list[T]:
    """``items`` in their original order without any item a newer item replaces."""
    replaced: set[str] = set()
    for index, first in enumerate(items):
        for second in items[index + 1 :]:
            if not same_statement_different_value(content(first), content(second)):
                continue
            older, newer = sorted((first, second), key=lambda item: (created_at(item), key(item)))
            if key(older) != key(newer):
                replaced.add(key(older))
    return [item for item in items if key(item) not in replaced]
