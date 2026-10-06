"""How user facts are shown in search results (Round 2 improvement 1). No model calls.

* ``short_message``: the user's own words a fact came from, at most 160 characters, cut on a word
  boundary with an ellipsis, so a reader (or the answering model) can check the fact against them.
* ``about``: who a fact is about. A name right after a relation word ("sister Nadia", "my manager is
  Rosa Haddad") makes it that person's fact; anything else is about the user.
* ``group_by_person``: keeps the returned set and rank order, except that facts about the same named
  person move up next to that person's highest-ranked fact. Facts about the user and non-fact hits
  stay where they were.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import TypeVar

MAX_SOURCE_CHARS = 160
USER = "user"

_RELATIONS = (
    "sister|brother|sibling|mum|mom|mother|dad|father|parent|son|daughter|child|kid|wife|husband|partner|"
    "girlfriend|boyfriend|fiancée|fiancee|fiancé|fiance|spouse|grandma|grandmother|grandad|grandpa|grandfather|"
    "aunt|auntie|uncle|cousin|niece|nephew|friend|best friend|flatmate|roommate|housemate|neighbour|neighbor|"
    "colleague|coworker|co-worker|manager|boss|mentor|teacher|tutor|coach|doctor|therapist"
)
# relation word (optionally plural), then optional "is/named/called/,", then a capitalised name
_ABOUT = re.compile(
    rf"\b(?:{_RELATIONS})s?\b,?\s+(?:(?:is|was|named|called)\s+)?([A-Z][a-zà-ÿ'’-]+)",
)
_LABEL = re.compile(r"^\[[^\]]*\]\s*")
_NOT_NAMES = frozenset({"The", "A", "An", "And", "But", "Who", "In", "At", "On", "Is", "Was", "He", "She", "They"})

T = TypeVar("T")


def short_message(text: str) -> str:
    """``text`` with whitespace collapsed, cut to 160 characters on a word boundary with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= MAX_SOURCE_CHARS:
        return flat
    room = MAX_SOURCE_CHARS - 1
    cut = flat.rfind(" ", 0, room + 1)
    head = flat[:cut] if cut > 0 else flat[:room]
    return head.rstrip(" ,;:") + "…"


def about(fact: str) -> str:
    """The named person a fact is about (first name after a relation word), else ``"user"``."""
    match = _ABOUT.search(_LABEL.sub("", fact, count=1))
    if match is None or match.group(1) in _NOT_NAMES:
        return USER
    return match.group(1)


def group_by_person(ranked: Sequence[T], *, who: Callable[[T], str | None]) -> list[T]:
    """Order-only: each named person's facts follow that person's highest-ranked fact.

    ``who`` returns the person for a fact hit, ``"user"`` for the user's own facts and ``None`` for
    hits that are not facts; only named people are grouped. Names match case-insensitively.
    """
    groups: dict[str, list[T]] = {}
    for item in ranked:
        person = who(item)
        if person is not None and person != USER:
            groups.setdefault(person.casefold(), []).append(item)
    out: list[T] = []
    placed: set[str] = set()
    for item in ranked:
        person = who(item)
        if person is None or person == USER:
            out.append(item)
            continue
        key = person.casefold()
        if key not in placed:
            placed.add(key)
            out.extend(groups[key])
    return out
