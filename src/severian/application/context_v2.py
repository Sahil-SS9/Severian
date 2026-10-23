"""v2 context format (SEVERIAN_CONTEXT_FORMAT=v2, off by default): the memory block the answerer gets.

Benchmarks showed Severian often FINDS the right memory but the answering model ignores it. The v2
block leads with the note most directly relevant to the question, marks every note current vs
historical, resolves each note's date, quotes the user's own words for user facts, and says plainly
what to do when nothing is relevant. Off (unset, or any other value): callers keep the v1 format,
byte for byte.

Ported onto 0.3.11 from `d956996` (feat/answer-memory-2026-10-09) for Handoff 2.

**Ordering with conflict notes** (on by default since 0.3.9): a note's state label — "earlier,
replaced" or "change, replaces an earlier note" — is the conflict note's information, attached to the
note it belongs to, so when v2 is on the trailing conflict note is not added as well. One voice, no
duplication, and nothing is lost: every note still says whether it is current and what replaced what.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

_SWITCH = "SEVERIAN_CONTEXT_FORMAT"
_V2 = "v2"

# The framing: one line each, constant size, so the block's overhead never grows with the store.
_LEAD = (
    "Notes from past conversations with the user — the note most directly relevant to this "
    "question is shown first."
)
_ABSTAIN = "If none of these notes is relevant to the question, say you don't know."

# One plain-words label per truth state (``TruthState``); the legacy default is ``current``.
_STATE_LABELS = {
    "current": "current",
    "concurrent": "also true together",
    "historical": "earlier, replaced",
    "transition": "change, replaces an earlier note",
}

_DATE_LABEL = re.compile(r"^\[(\d{4})/(\d{2})/(\d{2})")
_QUOTE_MAX_CHARS = 200


def context_v2_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``SEVERIAN_CONTEXT_FORMAT=v2`` switches the v2 block on; anything else keeps v1."""
    values = os.environ if env is None else env
    return values.get(_SWITCH, "").strip().lower() == _V2


def state_label(truth_state: str) -> str:
    """Plain words for a truth state; an unknown value is shown as it is, never hidden."""
    return _STATE_LABELS.get(truth_state, truth_state)


def said_date(content: str, created_at: datetime | None = None) -> str | None:
    """The resolved date a note is dated with: the content's own ``[YYYY/MM/DD ...]`` label first
    (that is when the user said it), the memory's created-at second. None when neither exists."""
    match = _DATE_LABEL.match(content)
    if match:
        return f"{match.group(1)}/{match.group(2)}/{match.group(3)}"
    if created_at is not None:
        return created_at.strftime("%Y/%m/%d")
    return None


def quote_of(text: str | None) -> str | None:
    """The compact evidence quote: the user's own words, cut on a word boundary. None when empty."""
    if not text:
        return None
    if len(text) <= _QUOTE_MAX_CHARS:
        return text
    cut = text[:_QUOTE_MAX_CHARS]
    space = cut.rfind(" ")
    if space > _QUOTE_MAX_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip() + "…"


@dataclass(frozen=True)
class ContextItem:
    """One memory as the v2 block shows it."""

    content: str
    state: str
    said: str | None = None
    quote: str | None = None
    sources: str = ""


class ContextV2Assembler:
    """Renders the v2 block: the relevance lead, numbered dated notes in the order given (the
    search order — most relevant first), then the don't-know rule.

    ``max_chars`` is a hard cap; None keeps every note's text whole. Either way the per-note
    annotations (state, date, quote, source) and the framing are bounded, so the block never
    grows without limit on its own.
    """

    def __init__(self, max_chars: int | None = None) -> None:
        if max_chars is not None and max_chars < 32:
            raise ValueError("max_chars must be at least 32 when set")
        self.max_chars = max_chars

    def assemble(self, items: Sequence[ContextItem]) -> str:
        if not items:
            return ""
        separator = "\n\n"
        if self.max_chars is None:
            body = separator.join(self._render(number, item, None) or "" for number, item in enumerate(items, 1))
            return separator.join((_LEAD, body, _ABSTAIN))
        # Reserve the framing first, so under a tight cap the rule survives and only note text is cut.
        budget = self.max_chars - len(_LEAD) - len(_ABSTAIN) - 2 * len(separator)
        notes: list[str] = []
        used = 0
        for number, item in enumerate(items, 1):
            before = len(separator) if notes else 0  # the separator between consecutive notes
            note = self._render(number, item, budget - used - before)
            if note is None:
                break
            notes.append(note)
            used += before + len(note)
        parts = [_LEAD, *notes, _ABSTAIN]
        block = separator.join(parts)
        return block if len(block) <= self.max_chars else block[: self.max_chars]

    def _render(self, number: int, item: ContextItem, room: int | None) -> str | None:
        head = f"{number}. [{item.state}" + (f", said {item.said}" if item.said else "") + "] "
        extras: list[str] = []
        if item.quote:
            extras.append(f'   the user said: "{item.quote}"')
        if item.sources:
            extras.append(f"   source: {item.sources}")
        if room is None:
            return "\n".join([head + item.content, *extras])
        content_room = room - len(head) - sum(1 + len(extra) for extra in extras)
        if content_room < 1:
            return None
        if len(item.content) > content_room:
            content = item.content[: content_room - 1].rstrip() + "…"
        else:
            content = item.content
        return "\n".join([head + content, *extras])
