"""Event time (Step 3, next-improvements plan): when it happened, as-of search, time words in questions."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from severian.application.eventtime import (
    TimeWindow,
    extract_event_span,
    extract_event_time,
    message_time,
    parse_time,
    query_window,
)
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Memory, Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="octacon", collection_id="profile", agent_id="octacon", session_id="shared"
)
REF = datetime(2026, 10, 4, 15, 30, tzinfo=UTC)  # a Sunday


def day(year: int, month: int, number: int) -> datetime:
    return datetime(year, month, number, tzinfo=UTC)


# -- date pick-out ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Met on 2026-08-14", day(2026, 8, 14)),
        ("We met on 14 August 2026", day(2026, 8, 14)),
        ("We met on 14th of August 2026", day(2026, 8, 14)),
        ("August 14, 2026 was great", day(2026, 8, 14)),
        ("Sept 3 meeting", day(2026, 9, 3)),
        ("25/12/2025 was the party", day(2025, 12, 25)),
        ("12/25/2025 was the party", day(2025, 12, 25)),
        ("Priya flew to Lisbon last Tuesday", day(2026, 9, 29)),
        ("Dinner next Friday", day(2026, 10, 9)),
        ("I saw her yesterday", day(2026, 10, 3)),
        ("We fly tomorrow", day(2026, 10, 5)),
        ("It happened today", day(2026, 10, 4)),
        ("three days ago we left", day(2026, 10, 1)),
        ("a week ago", day(2026, 9, 27)),
        ("2 months ago", day(2026, 8, 4)),
        ("in 2 weeks we go", day(2026, 10, 18)),
    ],
)
def test_dates_are_picked_out_in_utc(text, expected):
    assert extract_event_time(text, REF) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Born 2026-02-31",  # impossible day
        "The form says 03/04/2026",  # 3 April or 4 March: ambiguous
        "On 3 May and 5 June we travel",  # two different dates
        "Nothing to see here",
        "I may go there",  # "may" the verb, not the month
        "We will march on Monday",
        "The shop is open Mondays",
        "In August we hiked",  # a month is a period, not an exact day
    ],
)
def test_bad_ambiguous_or_missing_dates_are_left_empty(text):
    assert extract_event_time(text, REF) is None


def test_same_date_written_twice_is_still_one_date():
    assert extract_event_time("On 2026-08-14 (14 August 2026) we met", REF) == day(2026, 8, 14)


def test_capitalised_may_and_march_are_months():
    assert extract_event_time("Her exam was on 5 May 2026", REF) == day(2026, 5, 5)
    assert extract_event_time("They left on March 3 2026", REF) == day(2026, 3, 3)


def test_relative_dates_follow_the_reference_not_today():
    assert extract_event_time("last Tuesday", day(2025, 1, 15)) == day(2025, 1, 14)
    assert extract_event_time("yesterday", day(2025, 1, 1)) == day(2024, 12, 31)


def test_naive_reference_is_taken_as_utc():
    assert extract_event_time("yesterday", datetime(2026, 10, 4, 1, 0)) == day(2026, 10, 3)


def test_non_utc_reference_is_converted():
    from datetime import timedelta, timezone

    late = datetime(2026, 10, 4, 0, 30, tzinfo=timezone(timedelta(hours=2)))  # still 3 Oct in UTC
    assert extract_event_time("today", late) == day(2026, 10, 3)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Back in August we hiked", (day(2026, 8, 1), day(2026, 9, 1))),
        ("It will happen in August", (day(2027, 8, 1), day(2027, 9, 1))),
        ("July 2026 was hot", (day(2026, 7, 1), day(2026, 8, 1))),
        ("last month it rained", (day(2026, 9, 1), day(2026, 10, 1))),
        ("last week we moved", (day(2026, 9, 21), day(2026, 9, 28))),
        ("Met on 2026-08-14", (day(2026, 8, 14), day(2026, 8, 15))),
    ],
)
def test_periods_give_a_start_and_end(text, expected):
    assert extract_event_span(text, REF) == expected


def test_a_month_named_later_in_the_year_means_last_year_for_a_past_event():
    assert extract_event_span("We went in December", REF) == (day(2025, 12, 1), day(2026, 1, 1))


def test_conflicting_periods_are_left_empty():
    assert extract_event_span("In August, and again on 3 May 2026", REF) is None


# -- question time words ------------------------------------------------------


def test_query_window_for_a_month_a_year_and_relative_periods():
    assert query_window("What did I do in August?", REF) == TimeWindow(day(2026, 8, 1), day(2026, 9, 1))
    assert query_window("What happened in 2025?", REF) == TimeWindow(day(2025, 1, 1), day(2026, 1, 1))
    assert query_window("what happened last month", REF) == TimeWindow(day(2026, 9, 1), day(2026, 10, 1))
    assert query_window("what happened yesterday?", REF) == TimeWindow(day(2026, 10, 3), day(2026, 10, 4))


def test_query_window_before_and_after_a_date():
    assert query_window("anything before 3 May 2026", REF) == TimeWindow(None, day(2026, 5, 3))
    assert query_window("anything since March 2026", REF) == TimeWindow(day(2026, 3, 1), None)
    assert query_window("anything after 3 May 2026", REF) == TimeWindow(day(2026, 5, 4), None)


@pytest.mark.parametrize("query", ["Where does Alice work?", "How many days ago", "between May and June", ""])
def test_query_without_one_clear_time_has_no_window(query):
    assert query_window(query, REF) is None


def test_window_overlap_and_contains():
    window = TimeWindow(day(2026, 8, 1), day(2026, 9, 1))
    assert window.contains(day(2026, 8, 31)) and not window.contains(day(2026, 9, 1))
    assert window.overlaps(day(2026, 7, 31), day(2026, 8, 2)) and not window.overlaps(day(2026, 9, 1), day(2026, 9, 2))
    assert TimeWindow(None, None).overlaps(day(2000, 1, 1), day(2000, 1, 2))


def test_message_time_prefers_the_source_time():
    assert message_time({"timestamp": "2026-03-01T10:00:00Z"}, REF) == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert message_time({"date": "2026-03-01"}, REF) == day(2026, 3, 1)
    assert message_time({"timestamp": "garbage"}, REF) == REF
    assert message_time({}, REF) == REF
    assert parse_time(True) is None


# -- service ------------------------------------------------------------------


@pytest.fixture(params=["on", "off"])
def bundle(request, tmp_path: Path, monkeypatch):
    if request.param == "on":
        monkeypatch.setenv("SEVERIAN_EVENT_TIME", "1")
    else:
        monkeypatch.delenv("SEVERIAN_EVENT_TIME", raising=False)
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    built.param = request.param  # type: ignore[attr-defined]
    yield built
    built.close()


def say(bundle: ServiceBundle, when: str, *statements: str) -> None:
    for statement in statements:
        bundle.service.ingest_observation(SCOPE, content=statement, source={"session_id": "c", "timestamp": when})
        worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
        assert worker.drain(gateway=bundle.gateway).failed == 0


def memory_for(bundle: ServiceBundle, text: str) -> Memory:
    return next(memory for memory in bundle.repository.list_memories(SCOPE) if memory.content == text)


def test_event_time_is_stored_apart_from_save_time(bundle):
    say(bundle, "2026-09-30T09:00:00Z", "Priya flew to Lisbon last Tuesday")
    memory = memory_for(bundle, "Priya flew to Lisbon last Tuesday")
    if bundle.param == "on":
        assert memory.occurred_at == day(2026, 9, 29) and memory.occurred_until == day(2026, 9, 30)
        assert memory.created_at != memory.occurred_at
    else:
        assert memory.occurred_at is None and memory.occurred_until is None


def test_no_time_in_the_text_leaves_event_time_empty(bundle):
    say(bundle, "2026-09-30T09:00:00Z", "Alice likes green tea")
    memory = memory_for(bundle, "Alice likes green tea")
    assert memory.occurred_at is None and memory.occurred_until is None


def test_event_time_survives_a_store_round_trip(bundle):
    say(bundle, "2026-09-30T09:00:00Z", "The audit was on 2026-08-14")
    stored = bundle.repository.list_memories(SCOPE)[0]
    assert stored.occurred_at == (day(2026, 8, 14) if bundle.param == "on" else None)


def test_a_question_with_a_month_favours_memories_from_that_month(bundle):
    say(bundle, "2026-10-01T09:00:00Z", "Offsite hotel booking confirmed on 2026-03-10")
    say(bundle, "2026-10-01T09:00:00Z", "Offsite flight refund requested on 2026-08-14")
    hits = bundle.service.search(SCOPE, query="offsite hotel flight in August 2026", limit=5)
    top = hits[0].content
    if bundle.param == "on":
        assert top == "Offsite flight refund requested on 2026-08-14"
        assert hits[0].occurred_at == day(2026, 8, 14)
    assert {hit.content for hit in hits} >= {"Offsite hotel booking confirmed on 2026-03-10"}


def test_search_without_time_words_is_unchanged(bundle):
    say(bundle, "2026-10-01T09:00:00Z", "Offsite hotel booking confirmed on 2026-03-10")
    on_plain = [hit.content for hit in bundle.service.search(SCOPE, query="offsite hotel", limit=5)]
    assert on_plain == ["Offsite hotel booking confirmed on 2026-03-10"]


def test_as_of_search_answers_as_the_store_stood_then(bundle):
    say(bundle, "2026-05-01T09:00:00Z", "The meeting is on Tuesday")
    say(bundle, "2026-06-01T09:00:00Z", "The meeting is now on Thursday")
    then = [hit.content for hit in bundle.service.search(SCOPE, query="meeting", as_of=date(2026, 5, 15))]
    later = [hit.content for hit in bundle.service.search(SCOPE, query="meeting", as_of=date(2026, 6, 1))]
    assert then == ["The meeting is on Tuesday"]
    assert "The meeting is now on Thursday" in later


def test_as_of_before_everything_returns_nothing(bundle):
    say(bundle, "2026-05-01T09:00:00Z", "The meeting is on Tuesday")
    assert bundle.service.search(SCOPE, query="meeting", as_of=date(2026, 4, 30)) == []


def test_as_of_includes_the_whole_day_given(bundle):
    say(bundle, "2026-05-01T18:00:00Z", "The meeting is on Tuesday")
    assert len(bundle.service.search(SCOPE, query="meeting", as_of=date(2026, 5, 1))) == 1
    assert bundle.service.search(SCOPE, query="meeting", as_of=datetime(2026, 5, 1, 17, tzinfo=UTC)) == []


def test_no_as_of_means_everything(bundle):
    say(bundle, "2026-05-01T09:00:00Z", "The meeting is on Tuesday")
    assert len(bundle.service.search(SCOPE, query="meeting")) == 1
