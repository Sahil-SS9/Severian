"""Fix (d), ``SEVERIAN_TIE_ORDER``: when returned items share a time label, show which was said first.

A time label (``[2024/03/15 (Fri) 09:00]``) is the only sign of age the answering model sees. Round 3 (BEAM knowledge
update): BEAM stamps every message of a chat with the same date and 09:00, so in 9 of 10 questions at least 5 of the
10 items shared one label, and the old and new value of a fact ("deadline April 1" / "new deadline April 5") looked
equally recent (2 of Severian's 3 misses). Real Hermes labels are per minute, so turns in the same minute tie too.

Labels shared by two or more returned items get the message order among them:
``[2024/03/15 (Fri) 09:00, 2nd at this time]``. Items from the same message share a place; an item whose message
order is unknown is left as it was. Only labels that look like a time are touched. The stored memory never changes:
this is how results are shown. Off by default.
"""
from __future__ import annotations

import os
import re
from collections.abc import Sequence
from typing import Any

_LABEL = re.compile(r"^\[(?P<label>[^\]\n]{1,80})\]")
_LOOKS_LIKE_TIME = re.compile(r"\d{4}[/-]\d{1,2}[/-]\d{1,2}|\d{1,2}:\d{2}")


def tie_order_enabled() -> bool:
    """``SEVERIAN_TIE_ORDER`` switches fix (d) on. Off: labels are exactly as stored."""
    return os.environ.get("SEVERIAN_TIE_ORDER", "").strip().lower() in {"1", "true", "on", "yes"}


def ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th, ..., 11th, 12th, 13th, 21st, ..."""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def mark_ties(contents: Sequence[str], order: Sequence[tuple[Any, ...] | None]) -> list[str]:
    """``contents`` with the message order added to every time label that two or more of them share.

    ``order[i]`` is a sortable key for the message ``contents[i]`` came from (earlier messages sort first; equal
    keys mean the same message), or None when unknown.
    """
    if len(order) != len(contents):
        raise ValueError("order must give one key (or None) per content")
    groups: dict[str, list[tuple[int, tuple[Any, ...]]]] = {}
    for index, content in enumerate(contents):
        match = _LABEL.match(content)
        key = order[index]
        if match and key is not None and _LOOKS_LIKE_TIME.search(match.group("label")):
            groups.setdefault(match.group("label"), []).append((index, key))
    marked = list(contents)
    for label, members in groups.items():
        keys = sorted({key for _index, key in members})
        if len(keys) < 2:
            continue
        place = {key: position for position, key in enumerate(keys, start=1)}
        for index, key in members:
            rest = contents[index][len(label) + 2 :]
            marked[index] = f"[{label}, {ordinal(place[key])} at this time]{rest}"
    return marked
