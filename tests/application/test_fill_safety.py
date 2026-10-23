"""The fill must never bring back what the rest of the product promises to hide.

`SEVERIAN_FILL_BUDGET` reads neighbouring memories straight from storage instead of searching, so every filter the
search path applies has to be applied here too: truth resolution's closures, forgetting, and the profile boundary.
Each test carries a positive control proving the fill actually reaches the neighbour in question, so a "not in the
block" pass cannot be vacuous.

Shapes mirror Kensei's independent probes (`review-h1/probe_fill_safety.py`); these are the in-repo regressions.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

A = Scope(tenant_id="t", profile_id="alice", collection_id="profile", agent_id="a", session_id="s1")
B = Scope(tenant_id="t", profile_id="bob", collection_id="profile", agent_id="a", session_id="s1")
TURNS = (
    "user: My favourite drink is green tea.\nassistant: Noted.",
    "user: I booked the dentist for Tuesday.\nassistant: Okay.",
    "user: The project codename is Falcon.\nassistant: Got it, Falcon.",
    "user: My home city is London.\nassistant: London, noted.",
    "user: I adopted a cat named Miso.\nassistant: Lovely.",
)
QUERY = "What is the project codename?"


@pytest.fixture
def build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """A fresh store per call, with only the switches named in the call set (everything else cleared)."""
    built: list[ServiceBundle] = []

    def factory(*on: str) -> ServiceBundle:
        for name in [key for key in os.environ if key.startswith("SEVERIAN_")]:
            monkeypatch.delenv(name, raising=False)
        for name in on:
            monkeypatch.setenv(name, "1")
        where = tmp_path / f"store{len(built)}"
        where.mkdir()
        bundle = build_bundle(database=where / "s.db", fts=where / "s.fts", vectors=where / "s.vec")
        built.append(bundle)
        return bundle

    yield factory
    for bundle in built:
        bundle.close()


def save(bundle: ServiceBundle, scope: Scope, turns=TURNS, session: str = "s0001", prefix: str = "") -> None:
    for turn, content in enumerate(turns, 1):
        bundle.service.ingest_observation(
            scope,
            content=prefix + content,
            source={"session_id": session, "turn": turn, "timestamp": "2024-03-15T09:00:00+00:00"},
        )
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).failed == 0


def block(bundle: ServiceBundle, scope: Scope = A, query: str = QUERY) -> str:
    hits = bundle.service.search(scope, query=query, limit=1)
    assert hits and "Falcon" in hits[0].content, "the caller's own item is the codename turn"
    return bundle.service.assemble_context(scope, hits, max_chars=8_000, query=query)


def memory_with(bundle: ServiceBundle, scope: Scope, text: str):
    found = [item for item in bundle.repository.list_memories(scope) if text in item.content]
    assert len(found) == 1, (text, [item.content for item in found])
    return found[0]


def test_control_the_fill_reaches_the_neighbours(build) -> None:
    bundle = build("SEVERIAN_FILL_BUDGET")
    save(bundle, A)
    out = block(bundle)
    assert "London" in out and "dentist" in out, "positive control: the turns either side of Falcon are appended"


def test_closed_fact_is_not_appended_in_the_now_view(build) -> None:
    """Truth resolution closes a replaced fact; search's `now` view hides it and so must the fill."""
    bundle = build("SEVERIAN_FILL_BUDGET", "SEVERIAN_TRUTH_RESOLUTION")
    save(bundle, A)
    old, new = memory_with(bundle, A, "London"), memory_with(bundle, A, "Miso")
    assert bundle.repository.close_memory(A, old.id, valid_until=datetime.now(UTC), replaced_by_id=new.id)
    now_hits = bundle.service.search(A, query="home city London", limit=10)
    assert all("London" not in hit.content for hit in now_hits), "control: search's now view hides the closed fact"
    assert "London" not in block(bundle), "the fill must not put a closed fact back into a now context"
    assert "dentist" in block(bundle), "and it still appends the neighbours that are open"


def test_closed_fact_is_still_shown_when_truth_resolution_is_off(build) -> None:
    """With the switch off nothing hides closed memories, so the fill's behaviour is unchanged."""
    bundle = build("SEVERIAN_FILL_BUDGET")
    save(bundle, A)
    old, new = memory_with(bundle, A, "London"), memory_with(bundle, A, "Miso")
    assert bundle.repository.close_memory(A, old.id, valid_until=datetime.now(UTC), replaced_by_id=new.id)
    hits = bundle.service.search(A, query="home city London", limit=10)
    assert any("London" in hit.content for hit in hits), "control: with truth resolution off, search still shows it"
    assert "London" in block(bundle), "so the fill shows it too: this switch on its own changes nothing"


def test_a_shared_conversation_id_never_leaks_another_profile(build) -> None:
    """Both profiles use the conversation id s0001: the read is scope-bound, not session-bound."""
    bundle = build("SEVERIAN_FILL_BUDGET")
    save(bundle, A, session="s0001")
    save(bundle, B, session="s0001", prefix="[BOB] ")
    out = block(bundle, A)
    assert "London" in out, "positive control: Alice's own neighbours are appended"
    assert "[BOB]" not in out, "a shared conversation id must never pull in another profile's turns"


def test_a_forgotten_memory_is_not_appended(build) -> None:
    bundle = build("SEVERIAN_FILL_BUDGET")
    save(bundle, A)
    bundle.service.erase_memory(A, memory_with(bundle, A, "London").id, reason="user asked to forget")
    assert "London" not in block(bundle), "a forgotten memory must not come back through the fill"
    assert "dentist" in block(bundle), "positive control: the other neighbour is still appended"


def test_a_forgotten_turn_is_not_appended(build) -> None:
    bundle = build("SEVERIAN_FILL_BUDGET")
    save(bundle, A)
    observation_id = memory_with(bundle, A, "London").source_observation_ids[0]
    bundle.service.erase_observation(A, observation_id, reason="user asked to forget")
    assert "London" not in block(bundle), "a forgotten turn must not come back through the fill"
    assert "dentist" in block(bundle), "positive control: the other neighbour is still appended"


def test_nothing_is_read_when_the_budget_is_already_full(build, monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-room guard is a cost claim: with no room left, the fill must not touch the repository at all."""
    bundle = build("SEVERIAN_FILL_BUDGET")
    save(bundle, A)
    calls = {"reads": 0}
    for name in ("get_observation", "list_session_observations", "list_memories_for_observations"):
        original = getattr(bundle.repository, name)

        def counted(*args: object, __original=original, **kwargs: object) -> object:
            calls["reads"] += 1
            return __original(*args, **kwargs)

        monkeypatch.setattr(bundle.repository, name, counted)

    hits = bundle.service.search(A, query=QUERY, limit=1)
    tight = bundle.service.assemble_context(A, hits, max_chars=200, query=QUERY)
    assert len(tight) <= 200
    assert calls["reads"] == 0, "no room left: the fill must not read anything"

    roomy = bundle.service.assemble_context(A, hits, max_chars=8_000, query=QUERY)
    assert calls["reads"] > 0, "positive control: with room it does read"
    assert len(roomy) > len(tight)
