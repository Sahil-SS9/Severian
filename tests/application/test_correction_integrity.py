"""Corrected or erased facts never resurface (improvement 1).

Runs through the production composition (``build_bundle``: SQLite canonical
store, FTS5, vectors, rule-based graph extraction) and the production worker
(``DerivationWorker.drain``), as the Hermes gateways and the drain cron use
them. The gaps pinned here were first reproduced by the 3 Oct 2026
improvement assessment (section 4, "Source correction" and "Rollback
history"):

- a revised observation was never re-derived, so the corrected fact vanished
  instead of replacing the old one;
- graph facts derived from a corrected or erased observation stayed current;
- rollback hid the demoted revision from the revision history;
- erasing an observation must leave neither the fact nor a placeholder in
  any read path.
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


def drain(bundle: ServiceBundle) -> None:
    summary = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    assert summary.failed == 0 and summary.projection_failed == 0


def recalled(bundle: ServiceBundle, query: str, *, derive: bool = False) -> list[str]:
    hits = bundle.service.search(SCOPE, query=query, limit=20, derive=derive, gateway=bundle.gateway)
    return [hit.content for hit in hits]


def ingest(bundle: ServiceBundle, content: str) -> str:
    return bundle.service.ingest_observation(SCOPE, content=content, source={"session_id": "chat-1"}).observation_id


def current_facts(bundle: ServiceBundle, name: str) -> set[tuple[str, str, str]]:
    graph = GraphRetrievalService(bundle.repository)
    seed = graph.find_entity(SCOPE, name=name)
    if seed is None:
        return set()
    names = {entity.id: entity.name for entity in bundle.repository.list_entities(SCOPE)}
    neighbourhood = graph.neighborhood(SCOPE, seed.id)
    return {(names[r.subject_id], r.predicate, names[r.object_id]) for r in neighbourhood.relations}


# -- source correction --------------------------------------------------------


def test_revised_fact_replaces_the_old_one_after_the_next_drain(bundle):
    observation = ingest(bundle, "The deploy window is Thursday afternoon")
    drain(bundle)
    assert any("Thursday" in hit for hit in recalled(bundle, "deploy window"))

    bundle.service.revise_observation(SCOPE, observation, "The deploy window is Friday morning")
    drain(bundle)

    hits = recalled(bundle, "deploy window")
    assert any("Friday" in hit for hit in hits), hits
    assert not any("Thursday" in hit for hit in hits), hits


def test_revision_is_derived_exactly_once(bundle):
    observation = ingest(bundle, "The release train leaves at 09:00")
    drain(bundle)
    bundle.service.revise_observation(SCOPE, observation, "The release train leaves at 10:30")
    drain(bundle)
    drain(bundle)

    assert recalled(bundle, "release train").count("The release train leaves at 10:30") == 1


def test_graph_facts_from_a_corrected_source_are_retracted(bundle):
    observation = ingest(bundle, "Alice works at Acme")
    drain(bundle)
    assert ("Alice", "works_at", "Acme") in current_facts(bundle, "Alice")

    bundle.service.revise_observation(SCOPE, observation, "Alice works at Globex")
    drain(bundle)

    facts = current_facts(bundle, "Alice")
    assert ("Alice", "works_at", "Globex") in facts
    assert ("Alice", "works_at", "Acme") not in facts


def test_graph_fact_stays_while_another_source_still_supports_it(bundle):
    first = ingest(bundle, "Bob works at Initech")
    ingest(bundle, "Confirmed again today: Bob works at Initech")
    drain(bundle)

    bundle.service.erase_observation(SCOPE, first, reason="duplicate")
    drain(bundle)

    assert ("Bob", "works_at", "Initech") in current_facts(bundle, "Bob")


# -- erasure ------------------------------------------------------------------


@pytest.mark.parametrize("derive", [False, True])
def test_erased_observation_leaves_no_trace_in_search(bundle, derive):
    observation = ingest(bundle, "The vault code is 4711")
    drain(bundle)
    assert any("4711" in hit for hit in recalled(bundle, "vault code"))

    bundle.service.erase_observation(SCOPE, observation, reason="user asked to forget")
    drain(bundle)

    hits = recalled(bundle, "vault code forget", derive=derive)
    assert not any("4711" in hit for hit in hits), hits
    assert not any("forget" in hit for hit in hits), hits


def test_erased_observation_graph_facts_are_retracted(bundle):
    observation = ingest(bundle, "Carol works at Hooli")
    drain(bundle)
    bundle.service.erase_observation(SCOPE, observation, reason="wrong")
    drain(bundle)

    assert ("Carol", "works_at", "Hooli") not in current_facts(bundle, "Carol")


def test_erased_observation_is_not_pending_derivation(bundle):
    observation = ingest(bundle, "The office wifi password is hunter2")
    drain(bundle)
    bundle.service.erase_observation(SCOPE, observation, reason="secret")

    assert bundle.repository.list_pending_observations(SCOPE, limit=50) == []


# -- revision history ---------------------------------------------------------


def test_rollback_keeps_every_revision_in_history(bundle):
    ingest(bundle, "The standup is at 09:15")
    drain(bundle)
    (first,) = bundle.repository.list_memories(SCOPE)
    second = bundle.service.correct_memory(SCOPE, first.id, content="The standup is at 09:30",
                                           model_id="local/test", prompt_version="v2")
    third = bundle.service.correct_memory(SCOPE, second.id, content="The standup is at 10:00",
                                          model_id="local/test", prompt_version="v3")

    bundle.service.rollback_memory(SCOPE, second.id)

    history = bundle.repository.list_memory_revisions(SCOPE, second.id)
    assert [memory.id for memory in history] == [first.id, second.id, third.id]
    assert [hit for hit in recalled(bundle, "standup")] == ["The standup is at 09:30"]
