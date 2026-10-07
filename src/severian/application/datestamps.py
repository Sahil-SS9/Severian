"""Dates on memories (Round 2 improvement 2): stamp each memory with when it was said, and put a real date
next to relative words such as "last Tuesday".

Rules only, no model call, switched on by ``SEVERIAN_DATE_STAMPS``. Dates resolve against the time the
source message was written (``eventtime.message_time``), never against today. The relative words are kept
and the date goes in brackets right after them: "last Tuesday (2026/04/14 Tue)". Anything the event-time
rules call unclear (ambiguous numeric dates, bare weekdays, "a few days ago") is left as written. Explicit
dates ("3 May") are not touched: they are not relative, and their year is a guess.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

from severian.application.eventtime import _find, as_utc

_LABEL = re.compile(r"^\[[^\]\n]{1,80}\][ \t]*")
_RELATIVE = re.compile(r"\b(yesterday|today|tonight|tomorrow|last|next|this|ago)\b", re.I)
_WEEKEND = re.compile(r"\b(last|this|next) weekend\b", re.I)
_ALREADY = re.compile(r"^ \((?:around |week of )?\d{4}(?:/|\))")


def date_stamps_enabled() -> bool:
    """``SEVERIAN_DATE_STAMPS`` switches improvement 2 on. Off: memories are exactly as before."""
    return os.environ.get("SEVERIAN_DATE_STAMPS", "").strip().lower() in {"1", "true", "on", "yes"}


def stamp(when: datetime) -> str:
    """The usual time label: ``[2026/04/20 (Mon) 02:00]`` (UTC)."""
    return as_utc(when).strftime("[%Y/%m/%d (%a) %H:%M]")


def _day(value: date) -> str:
    return value.strftime("%Y/%m/%d %a")


def _range(first: date, last: date) -> str:
    tail = last.strftime("%d") if (first.year, first.month) == (last.year, last.month) else last.strftime("%m/%d")
    return f"{first.strftime('%Y/%m/%d')}-{tail}"


def _weekend(word: str, reference: datetime) -> str:
    today = as_utc(reference).date()
    saturday = today - timedelta(days=today.weekday()) + timedelta(days=5)
    saturday += timedelta(days={"last": -7, "this": 0, "next": 7}[word.lower()])
    return _range(saturday, saturday + timedelta(days=1))


def _period(start: datetime, end: datetime, precise: bool, phrase: str) -> str | None:
    first, days = start.date(), (end - start).days
    if precise:
        return f"around {first.strftime('%Y/%m/%d')}" if re.search(r"\bago\b", phrase, re.I) else _day(first)
    if days == 7 and first.weekday() == 0:
        return f"week of {first.strftime('%Y/%m/%d')}"
    if first.day == 1 and 28 <= days <= 31 and (end.date() - timedelta(days=1)).month == first.month:
        return first.strftime("%Y/%m")
    if first.month == 1 and first.day == 1 and days in (365, 366):
        return first.strftime("%Y")
    return None


def resolve(text: str, reference: datetime) -> str:
    """``text`` with a real date in brackets after each relative date it can resolve; the rest unchanged."""
    found, unusable = _find(text, as_utc(reference), query=False)
    if unusable:
        return text
    inserts: list[tuple[int, str]] = []
    for item in found:
        phrase = text[item.span[0] : item.span[1]]
        if not _RELATIVE.search(phrase):
            continue
        if as_utc(reference).weekday() == 6 and re.fullmatch(r"this week", phrase.strip(), re.I):
            continue  # on a Sunday "this week" may mean the one ending or the one coming: leave as written
        shown = _period(item.start, item.end, item.precise, phrase)
        if shown:
            inserts.append((item.span[1], shown))
    taken = {end for end, _ in inserts}
    for match in _WEEKEND.finditer(text):
        if match.end() not in taken:
            inserts.append((match.end(), _weekend(match.group(1), reference)))
    for end, shown in sorted(inserts, reverse=True):
        if _ALREADY.match(text[end:]):  # dated before: never add a second date
            continue
        text = f"{text[:end]} ({shown}){text[end:]}"
    return text


def dated(content: str, reference: datetime) -> str:
    """``content`` with a leading time label (kept if it has one) and its relative dates resolved."""
    match = _LABEL.match(content)
    label = match.group(0).strip() if match else stamp(reference)
    body = content[match.end() :] if match else content
    return f"{label} {resolve(body, reference)}"

