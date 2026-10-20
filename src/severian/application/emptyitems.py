"""Fix (c), ``SEVERIAN_DROP_EMPTY``: results that carry no information give their place to the next result.

Round 3 (LoCoMo multi-hop, 43 questions): a quarter of Severian's 10 slots held nothing the answer could use. Three
narrow kinds are treated as empty:

- a note that only names the user: "The user is Melanie.", "The user is named Caroline.";
- a note about the conversation, not the user: "The user asked Caroline about ...", "The user is asking ...";
- a saved message that is only a short question, with no reply: "Cool! What type of music do you play?".

A question with a statement in it ("I'm getting creative too, just learning the piano. What made you try pottery?")
is not empty, and neither is a question followed by the assistant's reply. A name question keeps name notes; a
question about what someone asked keeps asked notes and bare questions.

Empty results move behind every other result instead of being deleted, so they still show when nothing else is
left (a scope with few memories never loses results). Off by default.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from typing import TypeVar

T = TypeVar("T")

_LABEL = re.compile(r"^\[[^\]\n]{1,80}\][ \t]*")
_NAME = r"[A-Z][\w'’.-]*(?: [A-Z][\w'’.-]*){0,2}"
_NAME_ONLY = re.compile(rf"^The user(?:'s name is| is named| is called| is) {_NAME}\.?$")
_ASKED = re.compile(r"^The user (?:asked|asks|is asking|was asking|inquired|inquires|enquired|enquires)\b")
_SPEAKER = re.compile(r"^(?:user|[A-Z][\w'’.-]{0,30}(?: [A-Z][\w'’.-]{0,30})?): (?P<body>.*)$", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_ASKS_NAMES = re.compile(r"\b(?:name|named|called|who)\b", re.IGNORECASE)
_ASKS_ASKED = re.compile(r"\bask(?:s|ed|ing)?\b", re.IGNORECASE)
# A bare question is short; a sentence of up to 4 words beside it is a greeting ("Thanks, Melanie!"), not a fact.
_MAX_QUESTION_CHARS = 120
_MAX_GREETING_WORDS = 4


def drop_empty_enabled() -> bool:
    """``SEVERIAN_DROP_EMPTY`` switches fix (c) on. Off: results are exactly as before."""
    return os.environ.get("SEVERIAN_DROP_EMPTY", "").strip().lower() in {"1", "true", "on", "yes"}


def _bare_question(text: str) -> bool:
    match = _SPEAKER.match(text)
    if match is None:
        return False
    body = match.group("body").strip()
    if "\nassistant: " in body or len(body) >= _MAX_QUESTION_CHARS or not body.endswith("?"):
        return False
    sentences = [part for part in _SENTENCE_END.split(body) if part]
    return all(part.endswith("?") or len(part.split()) <= _MAX_GREETING_WORDS for part in sentences)


def is_empty(content: str, query: str | None = None) -> bool:
    """True when ``content`` tells the answer nothing (see the module notes); ``query`` can keep it."""
    text = _LABEL.sub("", content, count=1).strip()
    asked_about = query or ""
    if _NAME_ONLY.match(text):
        return _ASKS_NAMES.search(asked_about) is None
    if _ASKED.match(text) or _bare_question(text):
        return _ASKS_ASKED.search(asked_about) is None
    return False


def demote_empty(items: Sequence[T], *, content: Callable[[T], str], query: str | None = None) -> list[T]:
    """``items`` with the empty ones moved behind all the others; both groups keep their order."""
    kept: list[T] = []
    empty: list[T] = []
    for item in items:
        (empty if is_empty(content(item), query) else kept).append(item)
    return [*kept, *empty]
