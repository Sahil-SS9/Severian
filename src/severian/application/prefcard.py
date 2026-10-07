"""Preference card (Round 2 improvement 3): the user's stated preferences, returned with every search.

Rules only, no model call, switched on by ``SEVERIAN_PREFERENCE_CARD``. Built from user facts (Round 2
idea 1) that state a stable like, dislike, diet or allergy; goals ("wants to") and instructions for later
are left out. Newest first, no repeats, hard cap of about ``CARD_TOKEN_CAP`` tokens (words x 4/3), so the
card swaps old preferences out instead of growing. It sits outside the search results (approved 06/10).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

CARD_TOKEN_CAP = 500
CARD_TITLE = "The user's stated preferences (newest first):"
_TRUE = {"1", "true", "on", "yes"}
_LABEL = re.compile(r"^\[[^\]\n]{1,80}\]\s*")
_PREFERENCE = re.compile(
    r"^The user(?:'s favou?rite\b| (?:(?:really|strongly|generally|usually|always|still) )?"
    r"(?:prefers|likes|loves|enjoys|hates|dislikes|avoids|is (?:a fan of|vegetarian|vegan|pescatarian|allergic|"
    r"lactose|gluten|intolerant)|(?:does not|doesn't|never) (?:like|eat|drink|eats|drinks)|"
    r"can't stand|cannot stand)\b)",
    re.I,
)


def preference_card_enabled() -> bool:
    return os.environ.get("SEVERIAN_PREFERENCE_CARD", "").strip().lower() in _TRUE


def is_preference(fact: str) -> bool:
    """A user fact that states a stable preference about the user themself."""
    return bool(_PREFERENCE.match(_LABEL.sub("", fact).strip()))


def _tokens(text: str) -> int:
    return len(text.split()) * 4 // 3


def build_card(facts_oldest_first: Iterable[str]) -> str | None:
    """The card text, or None when there are no stated preferences."""
    lines: list[str] = []
    seen: set[str] = set()
    for fact in reversed(list(facts_oldest_first)):
        if not is_preference(fact):
            continue
        key = " ".join(_LABEL.sub("", fact).lower().split())
        if key in seen:
            continue
        line = f"- {fact.strip()}"
        if _tokens("\n".join([CARD_TITLE, *lines, line])) > CARD_TOKEN_CAP:
            break
        seen.add(key)
        lines.append(line)
    return "\n".join([CARD_TITLE, *lines]) if lines else None
