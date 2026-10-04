"""Newer facts win over older ones that contradict them (improvement 2).

Until now contradictions were only *reported* (dreaming / curation scans):
search kept returning both the old and the new version of a fact, and the
graph kept every value of a single-valued relation current. Corrections
(improvement 1) cover the case where someone says "that is wrong"; this
covers the far commoner case where the user simply states the new value.

Rules (deterministic, no model calls, nothing deleted):

- Search: when two returned memories are about the same subject (same
  leading content words) but end differently, only the newer one is
  returned. Unrelated memories that merely share words are untouched.
- Graph: for single-valued predicates (works_at, located_in, based_in) a
  newer value ends the older one (``valid_to``); history keeps it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from severian.application.graph import GraphRetrievalService
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

SCOPE = Scope(
    tenant_id="kensei",
    profile_id="octacon",
    collection_id="profile",
    agent_id="octacon",
    session_id="shared",
)


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def say(bundle: ServiceBundle, *statements: str) -> None:
    """Each statement is a separate turn, drained before the next (so it is strictly newer)."""
    for statement in statements:
        bundle.service.ingest_observation(SCOPE, content=statement, source={"session_id": "chat-1"})
        summary = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(
            gateway=bundle.gateway
        )
        assert summary.failed == 0


def recalled(bundle: ServiceBundle, query: str, limit: int = 20) -> list[str]:
    return [hit.content for hit in bundle.service.search(SCOPE, query=query, limit=limit)]


def current_facts(bundle: ServiceBundle, name: str) -> set[tuple[str, str, str]]:
    graph = GraphRetrievalService(bundle.repository)
    seed = graph.find_entity(SCOPE, name=name)
    if seed is None:
        return set()
    names = {entity.id: entity.name for entity in bundle.repository.list_entities(SCOPE)}
    relations = graph.neighborhood(SCOPE, seed.id).relations
    return {(names[r.subject_id], r.predicate, names[r.object_id]) for r in relations}


# -- search -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new", "query"),
    [
        ("The deploy window is Thursday afternoon", "The deploy window is Friday morning", "deploy window"),
        ("The release train leaves at 09:00", "The release train leaves at 10:30", "release train"),
        ("Sahil's preferred editor is Vim", "Sahil's preferred editor is Zed", "preferred editor"),
    ],
)
def test_newer_statement_replaces_the_older_one_in_search(bundle, old, new, query):
    say(bundle, old, new)

    hits = recalled(bundle, query)

    assert new in hits, hits
    assert old not in hits, hits


def test_order_decides_not_wording(bundle):
    say(bundle, "The deploy window is Friday morning", "The deploy window is Thursday afternoon")

    hits = recalled(bundle, "deploy window")

    assert "The deploy window is Thursday afternoon" in hits
    assert "The deploy window is Friday morning" not in hits


@pytest.mark.parametrize(
    ("first", "second", "query"),
    [
        ("Alice likes green tea", "Bob likes green tea", "green tea"),
        ("The staging database runs Postgres 16", "The production database runs Postgres 16", "Postgres 16"),
        ("The deploy window is Friday morning", "Remember to announce the deploy window in #ops", "deploy window"),
        ("The user prefers dark mode", "The user prefers green tea", "user prefers"),
    ],
)
def test_different_subjects_are_both_kept(bundle, first, second, query):
    say(bundle, first, second)

    hits = recalled(bundle, query)

    assert first in hits and second in hits, hits


def test_replaced_fact_does_not_eat_a_result_slot(bundle):
    say(bundle, "The standup is at 09:15", "The standup is at 09:30", "The retro is on Friday")

    assert set(recalled(bundle, "standup retro", limit=2)) == {"The standup is at 09:30", "The retro is on Friday"}


def test_older_fact_stays_in_history(bundle):
    say(bundle, "The deploy window is Thursday afternoon", "The deploy window is Friday morning")

    history = [memory.content for memory in bundle.repository.list_memories_history(SCOPE)]

    assert "The deploy window is Thursday afternoon" in history


# -- graph --------------------------------------------------------------------


def test_new_value_of_a_single_valued_relation_ends_the_old_one(bundle):
    say(bundle, "Alice works at Acme", "Alice works at Globex")

    facts = current_facts(bundle, "Alice")

    assert ("Alice", "works_at", "Globex") in facts
    assert ("Alice", "works_at", "Acme") not in facts


def test_ended_relation_is_kept_with_an_end_date(bundle):
    say(bundle, "Alice works at Acme", "Alice works at Globex")

    names = {entity.id: entity.name for entity in bundle.repository.list_entities(SCOPE)}
    ended = [
        r for r in bundle.repository.list_relations(SCOPE)
        if names[r.object_id] == "Acme" and r.predicate == "works_at"
    ]

    assert len(ended) == 1 and ended[0].valid_to is not None


def test_multi_valued_relations_accumulate(bundle):
    say(bundle, "Alice works with Bob", "Alice works with Carol")

    facts = current_facts(bundle, "Alice")

    assert {("Alice", "works_with", "Bob"), ("Alice", "works_with", "Carol")} <= facts


def test_restating_the_current_value_keeps_it_current(bundle):
    say(bundle, "Alice works at Acme", "Alice works at Acme again this year")

    assert ("Alice", "works_at", "Acme") in current_facts(bundle, "Alice")


def test_returning_to_an_earlier_value_makes_it_current_again(bundle):
    say(bundle, "Alice works at Acme", "Alice works at Globex", "Alice works at Acme again")

    facts = current_facts(bundle, "Alice")

    assert ("Alice", "works_at", "Acme") in facts
    assert ("Alice", "works_at", "Globex") not in facts
