"""Round 2 improvement 2, dates: every memory carries its date and weekday, and relative dates become real ones.

Rules only, no model calls, behind ``SEVERIAN_DATE_STAMPS`` (off = byte-identical to today). Only memories
and user facts change; the saved turn stays exactly what the user wrote. The relative words are kept and
the date is added in brackets after them, so a wrong guess never hides what was said.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from severian.application.datestamps import date_stamps_enabled, dated, stamp
from severian.application.userfacts import Extraction
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

SCOPE = Scope(tenant_id="kensei", profile_id="sahil", collection_id="profile", agent_id="kensei", session_id="s1")
MON = datetime(2026, 4, 20, 2, 0, tzinfo=UTC)  # a Monday


def test_the_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("SEVERIAN_DATE_STAMPS", raising=False)
    assert not date_stamps_enabled()
    monkeypatch.setenv("SEVERIAN_DATE_STAMPS", "1")
    assert date_stamps_enabled()


def test_stamp_is_the_date_weekday_and_time_in_the_usual_label_format():
    assert stamp(MON) == "[2026/04/20 (Mon) 02:00]"


def test_a_memory_without_a_label_is_stamped():
    assert dated("The user moved house.", MON) == "[2026/04/20 (Mon) 02:00] The user moved house."


def test_a_memory_that_already_has_a_label_keeps_it_and_is_not_stamped_twice():
    text = "[2023/05/20 (Sat) 10:00] The user moved house."
    assert dated(text, MON) == text


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("The user ran a half marathon last Tuesday.", "The user ran a half marathon last Tuesday (2026/04/14 Tue)."),
        ("The user got an Air Fryer yesterday.", "The user got an Air Fryer yesterday (2026/04/19 Sun)."),
        ("The user is off work tomorrow.", "The user is off work tomorrow (2026/04/21 Tue)."),
        ("The user flies next Friday.", "The user flies next Friday (2026/04/24 Fri)."),
        ("The user moved two weeks ago.", "The user moved two weeks ago (around 2026/04/06)."),
        ("The user started a new job last month.", "The user started a new job last month (2026/03)."),
        ("The user was ill last week.", "The user was ill last week (week of 2026/04/13)."),
        ("The user went to a BBQ last weekend.", "The user went to a BBQ last weekend (2026/04/18-19)."),
        ("The user is painting this weekend.", "The user is painting this weekend (2026/04/25-26)."),
    ],
)
def test_relative_dates_get_the_real_date_after_the_words(body, expected):
    assert dated(f"[2026/04/20 (Mon) 02:00] {body}", MON) == f"[2026/04/20 (Mon) 02:00] {expected}"


def test_the_date_comes_from_the_message_time_not_today():
    sunday = datetime(2023, 5, 21, 22, 54, tzinfo=UTC)
    assert dated("The user got an Air Fryer yesterday.", sunday) == (
        "[2023/05/21 (Sun) 22:54] The user got an Air Fryer yesterday (2023/05/20 Sat)."
    )


@pytest.mark.parametrize(
    "body",
    [
        "The user likes green tea.",  # no date at all
        "The user met Sam on 3 May 2026.",  # already a full date
        "The user met Sam on 3 May.",  # an explicit date, not a relative one: left alone
        "The user is going in August.",  # a month name, not relative
        "The user met Sam on 03/04/2026.",  # ambiguous day/month: never guessed
        "The user goes running on Tuesday.",  # a bare weekday: which one is unclear
        "The user did it a few days ago.",  # too vague to date
    ],
)
def test_explicit_vague_or_ambiguous_dates_are_left_as_written(body):
    assert dated(f"[2026/04/20 (Mon) 02:00] {body}", MON) == f"[2026/04/20 (Mon) 02:00] {body}"


def test_the_label_itself_is_never_read_as_a_date_in_the_text():
    text = "[2026/04/20 (Mon) 02:00] The user got a dog yesterday."
    assert dated(text, MON).count("(") == 2  # the label's weekday and one resolved date, nothing else


def test_two_relative_dates_are_each_resolved():
    assert dated("The user was ill yesterday and is off tomorrow.", MON) == (
        "[2026/04/20 (Mon) 02:00] The user was ill yesterday (2026/04/19 Sun) and is off tomorrow (2026/04/21 Tue)."
    )


def test_dating_twice_changes_nothing_more():
    once = dated("The user got a dog yesterday.", MON)
    assert dated(once, MON) == once


# -- wiring: derived memories and user facts --------------------------------------------------------------


class Facts:
    model = "fake-model"

    def extract(self, messages: list[str]) -> Extraction:
        return Extraction(facts={i: ["The user got an Air Fryer yesterday."] for i, _ in enumerate(messages, 1)})


def build(tmp_path: Path, monkeypatch, *, on: bool) -> ServiceBundle:
    monkeypatch.setenv("SEVERIAN_USER_FACTS", "1")
    if on:
        monkeypatch.setenv("SEVERIAN_DATE_STAMPS", "1")
    else:
        monkeypatch.delenv("SEVERIAN_DATE_STAMPS", raising=False)
    tmp_path.mkdir(parents=True, exist_ok=True)
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    built.derivation._fact_extractor = Facts()
    return built


def save_and_work(bundle: ServiceBundle, content: str) -> None:
    bundle.service.ingest_observation(
        SCOPE, content=content, source={"session_id": "s1", "turn": 1, "timestamp": "2023-05-21T22:54:00+00:00"}
    )
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).failed == 0
    assert worker.extract_facts().failed == 0


def contents(bundle: ServiceBundle) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for memory in bundle.repository.list_memories(SCOPE):
        out.setdefault("user_fact" if memory.kind == "user_fact" else "other", []).append(memory.content)
    return out


def test_with_the_switch_on_memories_and_facts_from_an_unlabelled_live_turn_are_dated(tmp_path, monkeypatch):
    bundle = build(tmp_path, monkeypatch, on=True)
    try:
        save_and_work(bundle, "user: I got an Air Fryer yesterday.\nassistant: Nice.")
        found = contents(bundle)
        assert found["user_fact"] == ["[2023/05/21 (Sun) 22:54] The user got an Air Fryer yesterday (2023/05/20 Sat)."]
        assert found["other"] and all(text.startswith("[2023/05/21 (Sun) 22:54] ") for text in found["other"])
        assert any("yesterday (2023/05/20 Sat)" in text for text in found["other"])
    finally:
        bundle.close()


def test_the_saved_turn_is_never_rewritten(tmp_path, monkeypatch):
    bundle = build(tmp_path, monkeypatch, on=True)
    try:
        save_and_work(bundle, "user: I got an Air Fryer yesterday.\nassistant: Nice.")
        (turn,) = bundle.repository.list_observations(SCOPE)
        assert turn.content == "user: I got an Air Fryer yesterday.\nassistant: Nice."
    finally:
        bundle.close()


def test_with_the_switch_off_memories_are_exactly_as_before(tmp_path, monkeypatch):
    bundle = build(tmp_path, monkeypatch, on=False)
    try:
        save_and_work(bundle, "user: I got an Air Fryer yesterday.\nassistant: Nice.")
        found = contents(bundle)
        assert found["user_fact"] == ["The user got an Air Fryer yesterday."]
        assert not any("(2023/05/20" in text or text.startswith("[2023") for text in found["other"])
    finally:
        bundle.close()


def test_event_time_is_the_same_with_or_without_the_dates(tmp_path, monkeypatch):
    spans = {}
    for on in (True, False):
        monkeypatch.setenv("SEVERIAN_EVENT_TIME", "1")
        bundle = build(tmp_path / str(on), monkeypatch, on=on)
        try:
            save_and_work(bundle, "user: I got an Air Fryer yesterday.\nassistant: Nice.")
            spans[on] = sorted(
                (m.kind, m.occurred_at, m.occurred_until) for m in bundle.repository.list_memories(SCOPE)
            )
        finally:
            bundle.close()
    assert spans[True] == spans[False] and any(span[1] is not None for span in spans[True])
