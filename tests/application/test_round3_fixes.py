"""Round 3 fixes wired into the service: search results and the context Severian hands the answering model.

Each fix has its own switch, and with every switch off the output is exactly as before: (a) SEVERIAN_REPLY_TRIM,
(b) SEVERIAN_CONFLICT_NOTES (on by default), (c) SEVERIAN_DROP_EMPTY. (d) SEVERIAN_TIE_ORDER was removed after the
21/10/2026 re-test: it changed no answers.
"""
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from severian.application.retrieval import ContextAssembler, RetrievalResult
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

SWITCHES = ("SEVERIAN_REPLY_TRIM", "SEVERIAN_CONFLICT_NOTES", "SEVERIAN_DROP_EMPTY")
A = Scope(tenant_id="t", profile_id="alice", collection_id="profile", agent_id="a", session_id="s1")
B = Scope(tenant_id="t", profile_id="bob", collection_id="profile", agent_id="a", session_id="s1")
LABEL = "[2024/03/15 (Fri) 09:00]"
OLD = f"{LABEL} user: My sprint 1 deadline is April 1, 2024.\nassistant: Great, I will plan the sprint around April 1."
NEW = f"{LABEL} user: Update: the new sprint 1 deadline is April 5, 2024.\nassistant: Noted, it moves to April 5."


@pytest.fixture
def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator:
    built: list[ServiceBundle] = []

    def factory(*on: str) -> ServiceBundle:
        for name in SWITCHES:
            if name in on:
                monkeypatch.setenv(name, "1")
            else:
                monkeypatch.setenv(name, "0")  # explicit: fix (b) is on by default
        where = tmp_path / f"store{len(built)}"
        where.mkdir()
        bundle = build_bundle(database=where / "s.db", fts=where / "s.fts", vectors=where / "s.vec")
        built.append(bundle)
        return bundle

    yield factory
    for bundle in built:
        bundle.close()


def save(bundle: ServiceBundle, scope: Scope, *contents: str, session: str = "s0001") -> None:
    for turn, content in enumerate(contents, 1):
        bundle.service.ingest_observation(
            scope,
            content=content,
            source={"session_id": session, "turn": turn, "timestamp": "2024-03-15T09:00:00+00:00"},
        )
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).failed == 0


def search(bundle: ServiceBundle, scope: Scope, query: str) -> list[str]:
    return [hit.content for hit in bundle.service.search(scope, query=query, limit=10)]


def context(bundle: ServiceBundle, scope: Scope, query: str, max_chars: int | None = None) -> str:
    hits = bundle.service.search(scope, query=query, limit=10)
    return bundle.service.assemble_context(scope, hits, max_chars=max_chars, query=query)


# -- (c) empty items give their place to the next result -----------------------------------------------------

MELANIE = f"{LABEL} Melanie: I play the clarinet in a jazz band with friends every weekend and love it."


def test_empty_items_move_behind_every_other_result(make) -> None:
    off = make()
    save(off, A, "The user is Melanie.", MELANIE)
    assert search(off, A, "Melanie") == ["The user is Melanie.", MELANIE]  # the empty note ranks first today
    on = make("SEVERIAN_DROP_EMPTY")
    save(on, A, "The user is Melanie.", MELANIE)
    assert search(on, A, "Melanie") == [MELANIE, "The user is Melanie."]


def test_a_name_question_keeps_the_name_note_in_place(make) -> None:
    on = make("SEVERIAN_DROP_EMPTY")
    save(on, A, "The user is Melanie.", MELANIE)
    assert search(on, A, "What is the name Melanie?")[0] == "The user is Melanie."


# -- (b) conflict notes ---------------------------------------------------------------------------------------

DENIAL = "The user has not completed any coin toss problems before."
CLAIM = "The user has already completed 5 coin toss problems with 80% mastery."
QUESTION = "Have I completed any coin toss problems before?"


def test_disagreeing_memories_get_a_note_after_the_results(make) -> None:
    bundle = make("SEVERIAN_CONFLICT_NOTES")
    save(bundle, A, DENIAL, CLAIM)
    assembled = context(bundle, A, QUESTION)
    note = assembled.rsplit("\n\n", 1)[1]
    assert note.startswith("Memories that disagree: ")
    assert DENIAL.rstrip(".") in note and CLAIM.rstrip(".") in note
    hits = bundle.service.search(A, query=QUESTION, limit=10)
    assert bundle.service.conflict_note(hits, query=QUESTION) == note


def test_the_note_fits_inside_the_space_limit(make) -> None:
    bundle = make("SEVERIAN_CONFLICT_NOTES")
    save(bundle, A, DENIAL + " " + "Extra words. " * 40, CLAIM + " " + "More words. " * 40)
    assembled = context(bundle, A, QUESTION, max_chars=600)
    assert len(assembled) <= 600
    assert assembled.rsplit("\n\n", 1)[1].startswith("Memories that disagree: ")


def test_conflict_notes_off_adds_nothing(make) -> None:
    bundle = make()
    save(bundle, A, DENIAL, CLAIM)
    assert "Memories that disagree" not in context(bundle, A, QUESTION)
    assert bundle.service.conflict_note(bundle.service.search(A, query=QUESTION, limit=10), query=QUESTION) is None


# -- (a) replies go first when the turns do not fit -----------------------------------------------------------

LONG_REPLY = "assistant: " + "Here is a long plan with many steps. " * 30
TURN1 = f"{LABEL} user: I moved to Leeds in May for the new job.\n{LONG_REPLY}"
TURN2 = f"{LABEL} user: I started at the Leeds office on 3 June.\n{LONG_REPLY}"


def test_when_the_turns_do_not_fit_the_replies_go_before_any_user_words(make) -> None:
    bundle = make("SEVERIAN_REPLY_TRIM")
    save(bundle, A, TURN1, TURN2)
    assembled = context(bundle, A, "When did I move to Leeds?", max_chars=700)
    assert "user: I moved to Leeds in May for the new job." in assembled
    assert "user: I started at the Leeds office on 3 June." in assembled
    assert "Here is a long plan" not in assembled
    assert len(assembled) <= 700


def test_when_everything_fits_the_replies_stay(make) -> None:
    bundle = make("SEVERIAN_REPLY_TRIM")
    save(bundle, A, TURN1, TURN2)
    assert context(bundle, A, "When did I move to Leeds?", max_chars=8_000).count("Here is a long plan") == 60


def test_a_question_about_what_was_said_keeps_the_replies(make) -> None:
    bundle = make("SEVERIAN_REPLY_TRIM")
    save(bundle, A, TURN1, TURN2)
    assert "Here is a long plan" in context(bundle, A, "What plan did you suggest for Leeds?", max_chars=700)


def test_reply_trim_off_cuts_as_before(make) -> None:
    bundle = make()
    save(bundle, A, TURN1, TURN2)
    hits = bundle.service.search(A, query="When did I move to Leeds?", limit=10)
    results = [
        RetrievalResult(
            memory=bundle.repository.get_memory(A, hit.memory_id),
            source_observation_ids=list(hit.source_observation_ids),
            lexical_score=hit.lexical_score,
            vector_score=hit.vector_score,
            fused_score=hit.fused_score,
        )
        for hit in hits
    ]
    expected = ContextAssembler(max_chars=700).assemble(results)
    assert context(bundle, A, "When did I move to Leeds?", max_chars=700) == expected
    assert "Here is a long plan" in expected


# -- every switch on: one profile never sees another's memories ----------------------------------------------


def test_with_every_switch_on_profiles_stay_separate(make) -> None:
    bundle = make(*SWITCHES)
    save(bundle, A, DENIAL, OLD)
    save(bundle, B, CLAIM, NEW, "The user is Bob.", session="s0002")
    for query in (QUESTION, "When is my sprint 1 deadline?"):
        hits = bundle.service.search(A, query=query, limit=10)
        assert {hit.scope for hit in hits} == {A}
        assembled = bundle.service.assemble_context(A, hits, query=query)
        for text in (CLAIM, "April 5", "Bob"):
            assert text not in assembled
        assert "Memories that disagree" not in assembled  # the other side of the conflict is Bob's
        assert "at this time]" not in assembled  # Bob's turn with the same label does not count
