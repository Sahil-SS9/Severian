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
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TypeVar

_WORD = re.compile(r"\w+", re.UNICODE)
_LINKING_WORDS = frozenset({"is", "are", "was", "were", "at", "on", "to", "now", "becomes", "equals"})
_MIN_SHARED_WORDS = 3

T = TypeVar("T")


def _words(text: str) -> list[str]:
    return [word.casefold() for word in _WORD.findall(text)]


def same_statement_different_value(a: str, b: str) -> bool:
    words_a, words_b = _words(a), _words(b)
    shared = 0
    for left, right in zip(words_a, words_b, strict=False):
        if left != right:
            break
        shared += 1
    if shared == len(words_a) or shared == len(words_b):
        return False  # one restates or extends the other: not a different value
    # Cut the shared opening back to its last linking word ("is at | 09 15").
    while shared and words_a[shared - 1] not in _LINKING_WORDS:
        shared -= 1
    shorter = min(len(words_a), len(words_b))
    return shared >= _MIN_SHARED_WORDS and shared * 2 >= shorter


def drop_replaced(
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
