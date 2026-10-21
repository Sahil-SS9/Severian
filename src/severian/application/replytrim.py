"""Fix (a), ``SEVERIAN_REPLY_TRIM``: drop the assistant's replies only when the returned turns do not fit.

A saved turn holds what the user said and the assistant's reply (``user: ...\\nassistant: ...``, optionally behind a
time label). The reply is usually most of the text (84% on LongMemEval), so under a tight space limit it pushes the
user's own facts out. Round 3 trial (20/10/2026, 60 LongMemEval questions): cutting every turn down to what the user
said took the 1,500-character score from 43% to 68%, but with no limit it cost points, because a reply can hold a
word the answer needs. So replies go only as far as needed, decided per context:

- everything fits: nothing changes;
- it does not fit: replies go one at a time from the lowest-ranked saved turn up, stopping as soon as everything
  fits, and all of them go before any of the user's words are cut;
- the question asks what was said ("what did you recommend", "remind me"): nothing changes.

The first version (0.3.8) cut every reply as soon as anything did not fit; at 8,000 characters on LongMemEval that
left ~74% of the space empty and lost 3 answers that sat in replies which would have fitted (re-run 21/10/2026).

Facts and anything else that is not a saved user turn are never touched. Off by default.
"""
from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence

# Optional time label, then the user's words; the reply starts at the first "\nassistant: ".
_TURN = re.compile(r"^(?:\[[^\]\n]{1,80}\][ \t]*)?user: ")
_REPLY = "\nassistant: "
# The question asks about the assistant's side of a past conversation.
_ASKS_REPLY = re.compile(
    r"\bremind me\b"
    r"|\byou (?:said|told|provided|gave|listed|suggested|recommended|mentioned|wrote|shared|explained)\b"
    r"|\bdid you (?:say|tell|provide|give|list|suggest|recommend|mention|write|share|explain)\b"
    r"|\bour (?:previous |earlier |last )?(?:conversation|chat|discussion)\b"
    r"|\bwe (?:discussed|talked about|went over)\b"
    r"|\bdid we (?:discuss|talk about|go over)\b",
    re.IGNORECASE,
)


def reply_trim_enabled() -> bool:
    """``SEVERIAN_REPLY_TRIM`` switches fix (a) on. Off: contexts are exactly as before."""
    return os.environ.get("SEVERIAN_REPLY_TRIM", "").strip().lower() in {"1", "true", "on", "yes"}


def asks_what_was_said(query: str | None) -> bool:
    """True when the question asks what the assistant said, so its replies are the evidence."""
    return bool(query) and _ASKS_REPLY.search(query or "") is not None


def without_reply(content: str) -> str:
    """A saved turn cut to its time label and the user's words; anything else unchanged."""
    if not _TURN.match(content):
        return content
    cut = content.find(_REPLY)
    return content[:cut] if cut > 0 else content


def fit_contents(
    contents: Sequence[str],
    budget: int,
    *,
    query: str | None = None,
    cost: Callable[[Sequence[str]], int] | None = None,
) -> list[str]:
    """``contents`` unchanged when they fit ``budget`` or the question asks what was said; else replies dropped from
    the last saved turn upward until they fit (all of them if nothing less will do).

    ``cost`` measures a list of contents (default: total characters). The caller still cuts whatever does not fit
    after the replies are gone, exactly as before.
    """
    measure = cost if cost is not None else (lambda items: sum(len(item) for item in items))
    items = list(contents)
    if measure(items) <= budget or asks_what_was_said(query):
        return items
    for index in range(len(items) - 1, -1, -1):
        cut = without_reply(items[index])
        if cut == items[index]:
            continue
        items[index] = cut
        if measure(items) <= budget:
            break
    return items
