"""Bounded neighbour lookups (review finding 04/10 on Step 2).

Conversation-neighbour expansion used to read EVERY observation and memory in the scope on every
search (about 160 ms per search at 5,000 memories, growing with the store). It now asks the store
for exactly what it needs: the observations of the matched conversations, and the memories made
from the neighbouring turns. Both backends must answer identically.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.sqlite import SQLiteRepository

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")


def _backend(kind: str, tmp_path: Path):
    if kind == "sqlite":
        backend = SQLiteRepository(tmp_path / "nb.db")
    else:
        if not PG_DSN:
            pytest.skip("SEVERIAN_PG_DSN not set; skipping live PostgreSQL neighbour lookups")
        from severian.infrastructure.postgresql import PostgreSQLBackend

        backend = PostgreSQLBackend(dsn=PG_DSN)
    backend.initialize()
    return backend


def _seed(repo):
    run = uuid.uuid4().hex[:8]  # unique ids per test: the live-PG test never wipes the database
    tenant, collection = f"t-nb-{run}", f"c-nb-{run}"
    scope = Scope(tenant_id=tenant, profile_id="p", collection_id=collection, agent_id="a", session_id="shared")
    other = Scope(tenant_id=tenant, profile_id="p2", collection_id=collection, agent_id="a2", session_id="shared")
    repo.create_collection(Collection(id=collection, tenant_id=tenant, name="nb"))
    observations: dict[str, Observation] = {}
    for owner in (scope, other):
        for session, turns in (("trip", 3), ("work", 2)):
            for turn in range(turns):
                obs = repo.add_observation(
                    Observation(
                        scope=owner,
                        content=f"{owner.profile_id} {session} turn {turn}",
                        source={"session_id": session, "turn": turn},
                    )
                )
                observations[f"{owner.profile_id}/{session}/{turn}"] = obs
    loose = repo.add_observation(Observation(scope=scope, content="no session here", source={}))
    observations["p/none"] = loose
    memories: dict[str, Memory] = {}
    for key, obs in observations.items():
        memories[key] = repo.add_memory(
            Memory(
                scope=obs.scope,
                kind="fact",
                content=f"memory of {obs.content}",
                source_observation_ids=[obs.id],
                model_id="m",
                prompt_version="p",
                confidence=0.9,
            )
        )
    return scope, observations, memories


@pytest.mark.parametrize("kind", ["sqlite", "postgresql"])
def test_session_observations_returns_only_those_conversations_in_scope(kind, tmp_path):
    repo = _backend(kind, tmp_path)
    try:
        SCOPE, observations, _ = _seed(repo)
        found = repo.list_session_observations(SCOPE, ["trip"])
        assert {o.id for o in found} == {observations[f"p/trip/{t}"].id for t in range(3)}
        both = repo.list_session_observations(SCOPE, ["trip", "work"])
        assert len(both) == 5
        assert repo.list_session_observations(SCOPE, []) == []
        assert repo.list_session_observations(SCOPE, ["missing"]) == []
    finally:
        repo.close()


@pytest.mark.parametrize("kind", ["sqlite", "postgresql"])
def test_memories_for_observations_returns_active_memories_in_scope(kind, tmp_path):
    repo = _backend(kind, tmp_path)
    try:
        SCOPE, observations, memories = _seed(repo)
        wanted = [observations["p/trip/0"].id, observations["p/trip/2"].id, observations["p2/trip/0"].id]
        found = repo.list_memories_for_observations(SCOPE, wanted)
        # the other profile's observation id never leaks across scope
        assert {m.id for m in found} == {memories["p/trip/0"].id, memories["p/trip/2"].id}
        assert repo.list_memories_for_observations(SCOPE, []) == []
    finally:
        repo.close()
