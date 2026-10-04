"""Event time: when something happened, found by rules (Step 3, next-improvements plan).

``created_at`` says when a memory was saved; ``occurred_at`` says when the thing it describes
happened ("Priya flew to Lisbon last Tuesday" -> that Tuesday). Both explicit dates and relative
dates are resolved against the time the source message was written, in UTC. Rules only, no
model call, and the same text with the same reference always gives the same answer.

Anything unclear is left empty instead of guessed: an impossible date (31 February), an
ambiguous numeric date (03/04/2026), or a text that names two different dates all give ``None``.

``query_window`` reads time words out of a question ("in August", "last month", "before 3 May
2026") so search can favour memories from that period.
"""

from __future__ import annotations

import calendar
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10,
    "nov": 11, "dec": 12,
}  # fmt: skip
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip
# "may" and "march" are also ordinary words: only read them as months when capitalised.
_AMBIGUOUS_MONTHS = frozenset({"may", "march"})
_FUTURE_MARKERS = re.compile(r"\b(will|going to|plan(?:s|ned|ning)? to|upcoming|scheduled|due|next)\b", re.I)

_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ORDINAL = r"(?:st|nd|rd|th)?"
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}}){_ORDINAL}\s+(?:of\s+)?({_MONTH})\b\.?(?:,?\s+(\d{{4}})\b)?", re.I)
_MONTH_DAY = re.compile(rf"\b({_MONTH})\b\.?\s+(\d{{1,2}}){_ORDINAL}(?!\d)(?:,?\s+(\d{{4}})\b)?", re.I)
_MONTH_YEAR = re.compile(rf"\b({_MONTH})\b\.?,?\s+(\d{{4}})\b", re.I)
_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_IN_MONTH = re.compile(rf"\b(last|next|this|in|during|since|until|by)\s+({_MONTH})\b", re.I)
_AGO = re.compile(r"\b(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
                  r"(day|week|month|year)s?\s+ago\b", re.I)  # fmt: skip
_IN_N = re.compile(r"\bin\s+(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+"
                   r"(day|week|month|year)s?\b", re.I)  # fmt: skip
_REL_DAY = re.compile(r"\b(yesterday|today|tonight|tomorrow)\b", re.I)
_REL_WEEKDAY = re.compile(r"\b(last|next)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_REL_PERIOD = re.compile(r"\b(last|next|this)\s+(week|month|year)\b", re.I)
_BARE_YEAR = re.compile(r"\b(?:in|during|of|from|since|before|after|until|by)\s+((?:19|20)\d{2})\b", re.I)
_DIRECTION = re.compile(r"\b(before|until|by|after|since|from)\s*$", re.I)


def event_time_enabled() -> bool:
    """``SEVERIAN_EVENT_TIME`` switches Step 3 on: event time at write, time words in questions at search."""
    return os.environ.get("SEVERIAN_EVENT_TIME", "").strip().lower() in {"1", "true", "on", "yes"}


@dataclass(frozen=True)
class _Found:
    start: datetime  # inclusive
    end: datetime  # exclusive: the end of the day / month / year the expression names
    span: tuple[int, int]
    precise: bool  # an exact day (usable as an event date), not a whole month/year/week


def as_utc(value: datetime) -> datetime:
    """``value`` in UTC; a naive time is taken to be UTC already."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def parse_time(value: Any) -> datetime | None:
    """A UTC datetime from an ISO string / datetime / epoch number; None when it is not one."""
    if isinstance(value, datetime):
        return as_utc(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return as_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


_SOURCE_TIME_KEYS = ("timestamp", "occurred_at", "message_time", "date", "time")


def message_time(source: dict[str, Any], default: datetime) -> datetime:
    """When the source message was written: a time in its ``source``, else ``default`` (save time)."""
    for key in _SOURCE_TIME_KEYS:
        found = parse_time(source.get(key))
        if found is not None:
            return found
    return as_utc(default)


def _midnight(year: int, month: int, day: int) -> datetime | None:
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def _add_months(value: datetime, months: int) -> datetime:
    index = value.year * 12 + (value.month - 1) + months
    year, month = divmod(index, 12)
    day = min(value.day, calendar.monthrange(year, month + 1)[1])
    return value.replace(year=year, month=month + 1, day=day)


def _month_window(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=UTC)
    return start, _add_months(start, 1)


def _day(value: datetime, *, span: tuple[int, int]) -> _Found:
    start = value.replace(hour=0, minute=0, second=0, microsecond=0)
    return _Found(start, start + timedelta(days=1), span, precise=True)


def _month_name(token: str, text: str, start: int) -> int | None:
    key = token.casefold().rstrip(".")
    if key in _AMBIGUOUS_MONTHS and not text[start : start + 1].isupper():
        return None
    return _MONTHS.get(key)


def _resolve_month(month: int, reference: datetime, *, future: bool, strictly_past: bool = False) -> tuple[int, int]:
    """(year, month) for a month named without a year, nearest to ``reference`` in the right direction."""
    if future:
        return (reference.year, month) if month >= reference.month else (reference.year + 1, month)
    if strictly_past:
        return (reference.year, month) if month < reference.month else (reference.year - 1, month)
    return (reference.year, month) if month <= reference.month else (reference.year - 1, month)


def _count(token: str) -> int:
    return int(token) if token.isdigit() else _NUMBER_WORDS[token.casefold()]


def _shift(reference: datetime, unit: str, amount: int) -> datetime:
    if unit == "day":
        return reference + timedelta(days=amount)
    if unit == "week":
        return reference + timedelta(weeks=amount)
    if unit == "month":
        return _add_months(reference, amount)
    return _add_months(reference, 12 * amount)


def _find(text: str, reference: datetime, *, query: bool) -> tuple[list[_Found], bool]:
    """Every time expression in ``text`` plus whether any expression was unusable (bad / ambiguous)."""
    future = bool(_FUTURE_MARKERS.search(text))
    found: list[_Found] = []
    unusable = False
    taken: list[tuple[int, int]] = []

    def free(span: tuple[int, int]) -> bool:
        return not any(span[0] < end and start < span[1] for start, end in taken)

    def add(item: _Found | None, span: tuple[int, int]) -> None:
        nonlocal unusable
        taken.append(span)
        if item is None:
            unusable = True
        else:
            found.append(item)

    for match in _ISO.finditer(text):
        day = _midnight(int(match[1]), int(match[2]), int(match[3]))
        add(_day(day, span=match.span()) if day else None, match.span())
    for match in _NUMERIC.finditer(text):
        if not free(match.span()):
            continue
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        if first > 12 and second <= 12:
            day = _midnight(year, second, first)
        elif second > 12 and first <= 12:
            day = _midnight(year, first, second)
        elif first == second:
            day = _midnight(year, first, second)
        else:
            day = None  # 03/04/2026 could be 3 April or 4 March: leave it empty
        add(_day(day, span=match.span()) if day else None, match.span())
    for match in _DAY_MONTH.finditer(text):
        if not free(match.span()):
            continue
        month = _month_name(match[2], text, match.start(2))
        if month is None:
            continue
        year = int(match[3]) if match[3] else _resolve_month(month, reference, future=future)[0]
        day = _midnight(year, month, int(match[1]))
        add(_day(day, span=match.span()) if day else None, match.span())
    for match in _MONTH_DAY.finditer(text):
        if not free(match.span()):
            continue
        month = _month_name(match[1], text, match.start(1))
        if month is None:
            continue
        year = int(match[3]) if match[3] else _resolve_month(month, reference, future=future)[0]
        day = _midnight(year, month, int(match[2]))
        add(_day(day, span=match.span()) if day else None, match.span())
    for match in _MONTH_YEAR.finditer(text):
        if not free(match.span()):
            continue
        month = _month_name(match[1], text, match.start(1))
        if month is None:
            continue
        start, end = _month_window(int(match[2]), month)
        add(_Found(start, end, match.span(), precise=False), match.span())
    for match in _IN_MONTH.finditer(text):
        if not free(match.span()):
            continue
        month = _month_name(match[2], text, match.start(2))
        if month is None:
            continue
        word = match[1].casefold()
        year, month = _resolve_month(
            month, reference, future=future or word == "next", strictly_past=word == "last"
        )
        start, end = _month_window(year, month)
        add(_Found(start, end, match.span(), precise=False), match.span())
    for match in _AGO.finditer(text):
        if free(match.span()):
            moment = _shift(reference, match[2].casefold(), -_count(match[1]))
            add(_day(moment, span=match.span()), match.span())
    for match in _IN_N.finditer(text):
        if free(match.span()):
            moment = _shift(reference, match[2].casefold(), _count(match[1]))
            add(_day(moment, span=match.span()), match.span())
    for match in _REL_DAY.finditer(text):
        if free(match.span()):
            word = match[1].casefold()
            offset = {"yesterday": -1, "tomorrow": 1}.get(word, 0)
            add(_day(reference + timedelta(days=offset), span=match.span()), match.span())
    for match in _REL_WEEKDAY.finditer(text):
        if not free(match.span()):
            continue
        target = _WEEKDAYS[match[2].casefold()]
        today = reference.weekday()
        delta = (today - target) % 7 or 7  # strictly before today
        moment = reference - timedelta(days=delta) if match[1].casefold() == "last" else (
            reference + timedelta(days=((target - today) % 7 or 7))
        )
        add(_day(moment, span=match.span()), match.span())
    for match in _REL_PERIOD.finditer(text):
        if not free(match.span()):
            continue
        direction = {"last": -1, "next": 1, "this": 0}[match[1].casefold()]
        unit = match[2].casefold()
        base = reference.replace(hour=0, minute=0, second=0, microsecond=0)
        if unit == "week":
            start = base - timedelta(days=base.weekday()) + timedelta(weeks=direction)
            end = start + timedelta(weeks=1)
        elif unit == "month":
            start = _add_months(base.replace(day=1), direction)
            end = _add_months(start, 1)
        else:
            start = datetime(base.year + direction, 1, 1, tzinfo=UTC)
            end = datetime(base.year + direction + 1, 1, 1, tzinfo=UTC)
        add(_Found(start, end, match.span(), precise=False), match.span())
    if query:
        for match in _BARE_YEAR.finditer(text):
            if free(match.span(1)):
                year = int(match[1])
                span = match.span(1)
                year_start, year_end = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
                add(_Found(year_start, year_end, span, precise=False), span)
    found.sort(key=lambda item: item.span)
    return found, unusable


def extract_event_span(text: str, reference: datetime) -> tuple[datetime, datetime] | None:
    """The period ``text`` says something happened in, as UTC ``[start, end)``; None when unclear.

    A day gives a one-day period, "in August" or "last week" a whole month or week. Relative words
    resolve against ``reference`` (the source message's time). A text naming two different periods,
    or any impossible or ambiguous date, gives None.
    """
    found, unusable = _find(text, as_utc(reference), query=False)
    periods = {(item.start, item.end) for item in found}
    if unusable or len(periods) != 1:
        return None
    return next(iter(periods))


def extract_event_time(text: str, reference: datetime) -> datetime | None:
    """The exact day ``text`` says something happened, in UTC; None unless it names exactly one day."""
    found, unusable = _find(text, as_utc(reference), query=False)
    precise = {item.start for item in found if item.precise}
    if unusable or len(precise) != 1 or any(not item.precise for item in found):
        return None
    return next(iter(precise))


@dataclass(frozen=True)
class TimeWindow:
    """A period a question asks about; ``None`` on either side means open-ended."""

    start: datetime | None
    end: datetime | None  # exclusive

    def contains(self, moment: datetime) -> bool:
        moment = as_utc(moment)
        return (self.start is None or moment >= self.start) and (self.end is None or moment < self.end)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """True when the period ``[start, end)`` shares any time with this window."""
        return (self.start is None or as_utc(end) > self.start) and (self.end is None or as_utc(start) < self.end)


def query_window(query: str, reference: datetime) -> TimeWindow | None:
    """The period a question asks about ("in August", "last month", "before 3 May 2026"), else None."""
    found, unusable = _find(query, as_utc(reference), query=True)
    if unusable or len({(item.start, item.end) for item in found}) != 1:
        return None
    item = found[0]
    direction = _DIRECTION.search(query[: item.span[0]])
    if direction is None:
        return TimeWindow(item.start, item.end)
    word = direction.group(1).casefold()
    if word in {"before", "until", "by"}:
        return TimeWindow(None, item.start if word == "before" else item.end)
    return TimeWindow(item.start if word in {"since", "from"} else item.end, None)
