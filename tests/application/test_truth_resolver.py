"""Handoff 1 (truth handling): a newer fact closes the older one it replaces, and nothing is deleted.

Switch: ``SEVERIAN_TRUTH_RESOLUTION`` (off by default). These tests drive the real stores through
the real worker, so what they prove is what the live pass does: saving turns, deriving memories,
extracting user facts, then searching the ``now`` / ``history`` / ``changes`` views.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from severian.application.truthresolution import TruthResolver
from severian.application.userfacts import Extraction
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Memory, Scope

SCOPE = Scope(tenant_id="kensei", profile_id="sahil", collection_id="profile", agent_id="kensei", session_id="shared")
OTHER = Scope(tenant_id="kensei", profile_id="misa", collection_id="profile", agent_id="kensei", session_id="shared")


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 1, day, hour, 0, tzinfo=UTC)


def make_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, on: bool = True, facts: bool = False
) -> ServiceBundle:
    for name in ("SEVERIAN_TRUTH_RESOLUTION", "SEVERIAN_TRUTH_RESOLUTION_URL", "SEVERIAN_USER_FACTS"):
        monkeypatch.delenv(name, raising=False)
    if on:
        monkeypatch.setenv("SEVERIAN_TRUTH_RESOLUTION", "1")
    if facts:
        monkeypatch.setenv("SEVERIAN_USER_FACTS", "1")
    tmp_path.mkdir(parents=True, exist_ok=True)
    return build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")


def say(
    bundle: ServiceBundle, user: str, when: datetime, *, assistant: str = "Noted!", scope: Scope = SCOPE, turn: int = 1
) -> None:
    label = when.strftime("[%Y/%m/%d (%a) %H:%M]")
    bundle.service.ingest_observation(
        scope,
        content=f"{label} user: {user}\nassistant: {assistant}",
        source={"session_id": f"s{when.day}", "turn": turn, "timestamp": when.isoformat()},
    )


def worker(bundle: ServiceBundle) -> DerivationWorker:
    return DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)


def drain(bundle: ServiceBundle) -> None:
    summary = worker(bundle).drain(gateway=bundle.gateway)
    assert summary.failed == 0 and summary.projection_failed == 0


def memory(bundle: ServiceBundle, text: str, scope: Scope = SCOPE, kind: str | None = None) -> Memory:
    matches = [
        m for m in bundle.repository.list_memories(scope) if text in m.content and (kind is None or m.kind == kind)
    ]
    assert len(matches) == 1, f"expected one memory containing {text!r}, found {len(matches)}"
    return matches[0]


def search(bundle: ServiceBundle, query: str, **kwargs):
    return bundle.service.search(kwargs.pop("scope", SCOPE), query=query, limit=10, **kwargs)


def contents(hits) -> str:
    return "\n".join(hit.content for hit in hits)


def home_move(bundle: ServiceBundle) -> None:
    say(bundle, "I live in Brighton.", at(24, 20))
    say(bundle, "We finally moved — I'm living in Bath now.", at(28, 4), assistant="Congratulations!")
    drain(bundle)


# -- the switch -----------------------------------------------------------------------------


def test_off_by_default_nothing_is_wired_and_nothing_is_closed(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=False)
    assert bundle.derivation.truth_resolver is None
    home_move(bundle)
    assert all(m.valid_until is None and m.replaced_by_id is None for m in bundle.repository.list_memories(SCOPE))
    both = contents(search(bundle, "live Brighton Bath"))
    assert "Brighton" in both and "Bath" in both  # exactly as before: both are still "current"
    bundle.close()


def test_switched_on_the_worker_derivation_has_a_resolver(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    assert isinstance(bundle.derivation.truth_resolver, TruthResolver)
    bundle.close()


# -- replace, and keep ----------------------------------------------------------------------


def test_a_new_home_closes_the_old_one_with_the_date_it_stopped_being_true(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    home_move(bundle)
    old, new = memory(bundle, "Brighton"), memory(bundle, "Bath")
    assert old.valid_until == at(28, 4)  # the day the newer message was written
    assert old.replaced_by_id == new.id
    assert new.valid_until is None and new.replaced_by_id is None
    bundle.close()


def test_now_returns_only_the_current_fact(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    home_move(bundle)
    now = contents(search(bundle, "live Brighton Bath"))
    assert "Bath" in now and "Brighton" not in now
    bundle.close()


def test_history_returns_both_with_dates_and_labels(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    home_move(bundle)
    found = search(bundle, "live Brighton Bath", view="history")
    hits = {("Brighton" if "Brighton" in h.content else "Bath"): h for h in found}
    assert set(hits) == {"Brighton", "Bath"}
    assert hits["Brighton"].truth_state == "historical"
    assert hits["Brighton"].valid_until == at(28, 4)
    assert hits["Brighton"].replaced_by_id == hits["Bath"].memory_id
    assert hits["Bath"].truth_state == "current" and hits["Bath"].valid_until is None
    assert any("Brighton" in earlier for earlier in hits["Bath"].previous)
    bundle.close()


def test_changes_shows_the_new_fact_with_what_it_replaced(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    home_move(bundle)
    hits = search(bundle, "live Brighton Bath", view="changes")
    assert [h.truth_state for h in hits] == ["transition"]
    assert "Bath" in hits[0].content and any("Brighton" in earlier for earlier in hits[0].previous)
    bundle.close()


def test_a_question_about_the_past_reads_the_history_view(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    home_move(bundle)
    assert "Brighton" in contents(search(bundle, "Which city did I live in before Bath? Brighton"))
    assert "Brighton" not in contents(search(bundle, "Which city do I live in? Brighton Bath"))
    bundle.close()


def test_a_question_about_the_past_is_untouched_when_the_switch_is_off(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=False)
    home_move(bundle)
    # Legacy behaviour: both are current, so both come back whatever the wording.
    assert "Brighton" in contents(search(bundle, "Which city do I live in? Brighton Bath"))
    bundle.close()


def test_as_of_a_date_before_the_move_still_sees_the_old_home(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    home_move(bundle)
    then = contents(search(bundle, "live Brighton Bath", as_of=date(2026, 1, 26)))
    assert "Brighton" in then and "Bath" not in then
    bundle.close()


def test_no_fact_is_lost(tmp_path, monkeypatch):
    off = make_bundle(tmp_path / "off", monkeypatch, on=False)
    home_move(off)
    on = make_bundle(tmp_path / "on", monkeypatch)
    home_move(on)
    before = sorted(m.content for m in off.repository.list_memories_history(SCOPE))
    after = sorted(m.content for m in on.repository.list_memories_history(SCOPE))
    assert before == after and len(after) >= 2  # same count, same words: closing deletes nothing
    for m in on.repository.list_memories(SCOPE):
        assert on.repository.get_memory(SCOPE, m.id).content == m.content
    off.close()
    on.close()


# -- both are true --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first", "second", "word_one", "word_two"),
    [
        ("Our cat is called Miso.", "Our rabbit is called Clover.", "Miso", "Clover"),
        ("I speak German.", "I speak Polish.", "German", "Polish"),
        ("I enjoy jazz.", "I enjoy rock.", "jazz", "rock"),
        ("My car is a MG4.", "My car is a Ford Puma.", "MG4", "Puma"),
        ("My sister Grace lives in Nottingham.", "My sister Nadia lives in Belfast.", "Nottingham", "Belfast"),
    ],
)
def test_both_true_facts_stay_current(tmp_path, monkeypatch, first, second, word_one, word_two):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, first, at(10))
    say(bundle, second, at(20))
    drain(bundle)
    assert memory(bundle, word_one).valid_until is None
    assert memory(bundle, word_two).valid_until is None
    bundle.close()


def test_a_job_change_and_a_gym_change_each_close_only_their_own_kind(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "I'm a member at Third Space.", at(5))
    say(bundle, "I just started a new job at Starling.", at(6))
    say(bundle, "I cancelled Third Space and signed up at Anytime Fitness instead.", at(20))
    say(bundle, "I've left Starling and joined Rightmove this week.", at(21))
    drain(bundle)
    assert memory(bundle, "I'm a member at Third Space").valid_until == at(20)
    assert memory(bundle, "new job at Starling").valid_until == at(21)
    assert memory(bundle, "signed up at Anytime Fitness").valid_until is None
    assert memory(bundle, "joined Rightmove").valid_until is None
    bundle.close()


def test_selling_a_car_closes_it_but_owning_two_does_not(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "My car is a MG4.", at(3))
    say(bundle, "My car is a Mazda CX-5.", at(8))  # two cars: both current
    say(bundle, "Sold the MG4; my new car is a Ford Puma.", at(20))
    drain(bundle)
    assert memory(bundle, "MG4.").valid_until == at(20)
    assert memory(bundle, "Mazda").valid_until is None
    assert memory(bundle, "Ford Puma").valid_until is None
    bundle.close()


# -- what must never be closed --------------------------------------------------------------


def test_an_old_message_with_other_content_is_not_closed(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "I live in Brighton. My dentist appointment is on the 14th of April at ten past nine.", at(24, 20))
    say(bundle, "We finally moved — I'm living in Bath now.", at(28, 4))
    drain(bundle)
    assert memory(bundle, "dentist").valid_until is None  # it also says something we cannot read
    bundle.close()


def test_an_old_message_with_a_long_assistant_reply_is_not_closed(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    long_reply = " ".join(f"detail{i}" for i in range(60))
    say(bundle, "I live in Brighton.", at(24, 20), assistant=long_reply)
    say(bundle, "We finally moved — I'm living in Bath now.", at(28, 4))
    drain(bundle)
    assert memory(bundle, "detail5 ").valid_until is None
    bundle.close()


def test_an_older_message_saved_later_never_closes_a_newer_one(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "We finally moved — I'm living in Bath now.", at(28, 4))
    say(bundle, "I live in Brighton.", at(24, 20))  # written earlier, saved later
    drain(bundle)
    assert memory(bundle, "Bath").valid_until is None
    bundle.close()


def test_the_result_does_not_depend_on_the_order_the_turns_were_saved(tmp_path, monkeypatch):
    """Newest saved first: the worker may drain in any order, and the old home must still end up closed."""
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "We finally moved — I'm living in Bath now.", at(28, 4), assistant="Congratulations!")
    say(bundle, "I live in Brighton.", at(24, 20))  # written earlier, saved later
    drain(bundle)
    old, new = memory(bundle, "Brighton"), memory(bundle, "Bath")
    assert old.valid_until == at(28, 4) and old.replaced_by_id == new.id
    assert new.valid_until is None
    now = contents(search(bundle, "live Brighton Bath"))
    assert "Bath" in now and "Brighton" not in now
    bundle.close()


def test_two_messages_at_the_same_moment_do_not_close_each_other(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "I live in Brighton.", at(24, 20), turn=1)
    say(bundle, "I'm living in Bath now.", at(24, 20), turn=2)
    drain(bundle)
    assert memory(bundle, "Brighton").valid_until is None and memory(bundle, "Bath").valid_until is None
    bundle.close()


def test_a_question_or_a_wish_never_closes_anything(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "I live in Brighton.", at(24, 20))
    say(bundle, "Where do I live? I might move to Leeds.", at(28, 4))
    drain(bundle)
    assert memory(bundle, "Brighton").valid_until is None
    bundle.close()


def test_running_the_resolver_again_changes_nothing(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    home_move(bundle)
    old, new = memory(bundle, "Brighton"), memory(bundle, "Bath")
    assert bundle.derivation.truth_resolver.resolve(SCOPE, [new]) == []
    again = memory(bundle, "Brighton")
    assert (again.valid_until, again.replaced_by_id) == (old.valid_until, old.replaced_by_id)
    bundle.close()


@pytest.mark.skipif(not os.environ.get("SEVERIAN_PG_DSN"), reason="SEVERIAN_PG_DSN not set")
def test_the_same_flow_works_on_postgresql(monkeypatch):
    """End to end on the live Postgres backend, in a tenant of its own (the shared test database is not reset)."""
    for name in ("SEVERIAN_TRUTH_RESOLUTION_URL", "SEVERIAN_USER_FACTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SEVERIAN_TRUTH_RESOLUTION", "1")
    monkeypatch.setenv("SEVERIAN_PG_ALLOW_RESET", "1")
    scope = SCOPE.model_copy(
        update={"tenant_id": f"t-{uuid.uuid4().hex[:8]}", "collection_id": f"c-{uuid.uuid4().hex[:8]}"}
    )
    bundle = build_bundle(backend="postgresql", dsn=os.environ["SEVERIAN_PG_DSN"])
    try:
        say(bundle, "I live in Brighton.", at(24, 20), scope=scope)
        say(bundle, "We finally moved — I'm living in Bath now.", at(28, 4), scope=scope)
        drain(bundle)
        old, new = memory(bundle, "Brighton", scope), memory(bundle, "Bath", scope)
        assert old.valid_until == at(28, 4) and old.replaced_by_id == new.id and new.valid_until is None
        now = contents(search(bundle, "live Brighton Bath", scope=scope))
        assert "Bath" in now and "Brighton" not in now
        hist = {h.truth_state for h in search(bundle, "live Brighton Bath", scope=scope, view="history")}
        assert hist == {"historical", "current"}
    finally:
        bundle.close()


# -- profiles stay apart --------------------------------------------------------------------


def test_one_profile_never_closes_or_reads_another_profiles_facts(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch)
    say(bundle, "I live in Brighton.", at(24, 20), scope=SCOPE)
    say(bundle, "We finally moved — I'm living in Bath now.", at(28, 4), scope=OTHER)
    drain(bundle)
    assert memory(bundle, "Brighton", SCOPE).valid_until is None
    assert "Brighton" in contents(search(bundle, "live Brighton Bath", scope=SCOPE))
    assert "Bath" not in contents(search(bundle, "live Brighton Bath", scope=SCOPE))
    assert "Brighton" not in contents(search(bundle, "live Brighton Bath", scope=OTHER, view="history"))
    bundle.close()


# -- user facts and the model ---------------------------------------------------------------


class FakeExtractor:
    model = "fake-model"

    def __init__(self, facts_for) -> None:
        self._facts_for = facts_for

    def extract(self, messages: list[str]) -> Extraction:
        return Extraction(facts={i: f for i, text in enumerate(messages, 1) if (f := self._facts_for(text))})


class ScriptedModel:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def fact_pass(bundle: ServiceBundle, facts_for) -> None:
    bundle.derivation._fact_extractor = FakeExtractor(facts_for)
    summary = worker(bundle).extract_facts()
    assert summary.failed == 0 and summary.projection_failed == 0


def seat_facts(text: str) -> list[str]:
    if "aisle" in text:
        return ["The user prefers the aisle seat."]
    if "window" in text:
        return ["The user prefers the window seat."]
    return []


def test_extracted_facts_are_resolved_too(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, facts=True)
    home_move(bundle)

    def facts_for(text: str) -> list[str]:
        return ["The user lives in Brighton."] if "Brighton" in text else ["The user lives in Bath."]

    fact_pass(bundle, facts_for)
    old = memory(bundle, "lives in Brighton", kind="user_fact")
    new = memory(bundle, "lives in Bath", kind="user_fact")
    assert old.valid_until == at(28, 4)
    assert new.valid_until is None
    # Replaced by an open memory that carries the newer value (the Bath turn, or the Bath fact).
    replacer = bundle.repository.get_memory(SCOPE, old.replaced_by_id)
    assert "Bath" in replacer.content and replacer.valid_until is None
    bundle.close()


def test_the_model_decides_what_the_rules_cannot(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, facts=True)
    model = ScriptedModel('{"decisions": [{"n": 1, "decision": "replace"}]}')
    bundle.derivation.truth_resolver.model = model
    say(bundle, "I always book the aisle seat.", at(10))
    say(bundle, "These days I always book the window seat.", at(20))
    drain(bundle)
    assert model.prompts == []  # raw turns are never sent to the model
    fact_pass(bundle, seat_facts)
    assert len(model.prompts) == 1
    assert memory(bundle, "aisle seat", kind="user_fact").valid_until == at(20)
    assert memory(bundle, "window seat", kind="user_fact").valid_until is None
    bundle.close()


@pytest.mark.parametrize(
    "reply", ["not json", '{"decisions": [{"n": 1, "decision": "delete"}]}', RuntimeError("down"), TimeoutError("slow")]
)
def test_any_model_failure_keeps_both_facts(tmp_path, monkeypatch, reply):
    bundle = make_bundle(tmp_path, monkeypatch, facts=True)
    bundle.derivation.truth_resolver.model = ScriptedModel(reply)
    say(bundle, "I always book the aisle seat.", at(10))
    say(bundle, "These days I always book the window seat.", at(20))
    drain(bundle)
    fact_pass(bundle, seat_facts)
    assert memory(bundle, "aisle seat", kind="user_fact").valid_until is None
    assert memory(bundle, "window seat", kind="user_fact").valid_until is None
    bundle.close()


# -- the sheet ------------------------------------------------------------------------------


def test_every_decision_is_written_to_the_log_when_asked(tmp_path, monkeypatch):
    log = tmp_path / "sheet.jsonl"
    monkeypatch.setenv("SEVERIAN_TRUTH_RESOLUTION_LOG", str(log))
    bundle = make_bundle(tmp_path / "b", monkeypatch)
    say(bundle, "Our cat is called Miso.", at(2))
    say(bundle, "Our rabbit is called Clover.", at(3))
    home_move(bundle)
    lines = [json.loads(line) for line in log.read_text().splitlines()]
    replaced = [line for line in lines if line["replaces"]]
    added = [line for line in lines if not line["replaces"]]
    assert len(replaced) == 1 and added  # one replacement, and the both-true pair is on the sheet too
    row = replaced[0]
    assert {"new_id", "old_id", "new_text", "old_text", "replaces", "method", "slot", "reason", "new_sources",
            "old_sources", "valid_until", "scope"} <= set(row)
    assert "Bath" in row["new_text"] and "Brighton" in row["old_text"]
    assert row["method"] == "rule" and row["slot"] == "home"
    assert any("Miso" in line["old_text"] and "Clover" in line["new_text"] for line in added)
    bundle.close()
