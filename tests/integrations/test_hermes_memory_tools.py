"""Agents can correct and forget memories through the Hermes tools (improvement 1).

Before this, an agent had search, ingest, get-observation and shared search
only: when the user said "that's wrong" or "forget that", nothing could
change what Severian would recall. ``severian_correct`` and
``severian_forget`` route to the engine's correction and erasure paths, with
the same exact-scope check as every other tool. Like turn sync, they are
refused outside the primary context (cron, subagent, flush), as is
``severian_ingest``: an unattended run must never rewrite a profile's memory.

Runs on the production composition and worker, so "recalled" means what a
gateway would actually surface after the drain cron has run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.integrations.hermes import SeverianMemoryProvider

SCOPE_ARGS = {
    "tenant_id": "kensei",
    "profile_id": "octacon",
    "collection_id": "profile",
    "agent_id": "octacon",
    "session_id": "shared",
}


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def provider(bundle: ServiceBundle, identity: str = "octacon", chat: str = "chat-1", context: str = "primary"):
    instance = SeverianMemoryProvider(service=bundle.service, gateway=bundle.gateway)
    instance.initialize(
        chat,
        platform="discord",
        hermes_home=f"/hermes/profiles/{identity}",
        agent_context=context,
        agent_identity=identity,
        agent_workspace="hermes",
    )
    return instance


def call(instance: SeverianMemoryProvider, tool: str, scope: dict | None = None, **args) -> dict:
    return json.loads(instance.handle_tool_call(tool, {**(scope or SCOPE_ARGS), **args}))


def drain(bundle: ServiceBundle) -> None:
    summary = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    assert summary.failed == 0 and summary.projection_failed == 0


def remember(bundle: ServiceBundle, text: str) -> dict:
    """Write one turn as a gateway would, drain, and return the search hit for it."""
    provider(bundle).sync_turn(text, "", session_id="chat-1")
    drain(bundle)
    (hit,) = [h for h in call(provider(bundle), "severian_search", query=text)["hits"] if h["content"] == text]
    return hit


def recall(bundle: ServiceBundle, query: str) -> str:
    later = provider(bundle, chat="chat-2")
    return later.prefetch(query, session_id="chat-2") + json.dumps(call(later, "severian_search", query=query))


def test_correct_and_forget_tools_are_offered():
    names = {schema["name"]: schema for schema in SeverianMemoryProvider().get_tool_schemas()}
    for name in ("severian_correct", "severian_forget"):
        assert set(SCOPE_ARGS) <= set(names[name]["parameters"]["required"])
        assert {"observation_id", "memory_id"} <= set(names[name]["parameters"]["properties"])
    assert "content" in names["severian_correct"]["parameters"]["required"]


def test_system_prompt_tells_the_agent_how_to_correct_and_forget(bundle):
    block = provider(bundle).system_prompt_block()
    assert "severian_correct" in block and "severian_forget" in block


def test_forget_by_observation_removes_the_fact_everywhere(bundle):
    hit = remember(bundle, "The vault code is 4711")

    result = call(provider(bundle), "severian_forget", observation_id=hit["source_observation_ids"][0],
                  reason="user asked to forget")
    drain(bundle)

    assert result == {"forgotten": "observation", "id": hit["source_observation_ids"][0]}
    context = recall(bundle, "vault code forget")
    assert "4711" not in context and "user asked to forget" not in context


def test_forget_by_memory_id(bundle):
    hit = remember(bundle, "The spare key is under the blue pot")

    result = call(provider(bundle), "severian_forget", memory_id=hit["memory_id"])
    drain(bundle)

    assert result == {"forgotten": "memory", "id": hit["memory_id"]}
    assert "blue pot" not in recall(bundle, "spare key")


def test_correct_observation_replaces_the_fact_after_the_next_drain(bundle):
    hit = remember(bundle, "The deploy window is Thursday afternoon")

    result = call(provider(bundle), "severian_correct", observation_id=hit["source_observation_ids"][0],
                  content="The deploy window is Friday morning")
    drain(bundle)

    assert result["corrected"] == "observation" and result["revision_of"] == hit["source_observation_ids"][0]
    context = recall(bundle, "deploy window")
    assert "Friday morning" in context and "Thursday" not in context


def test_correct_memory_replaces_the_fact_at_once(bundle):
    hit = remember(bundle, "The standup is at 09:15")

    result = call(provider(bundle), "severian_correct", memory_id=hit["memory_id"], content="The standup is at 09:30")

    assert result["corrected"] == "memory" and result["revision_of"] == hit["memory_id"]
    context = recall(bundle, "standup")
    assert "09:30" in context and "09:15" not in context


@pytest.mark.parametrize("tool", ["severian_correct", "severian_forget"])
@pytest.mark.parametrize("targets", [{}, {"observation_id": "o", "memory_id": "m"}])
def test_mutation_tools_need_exactly_one_target(bundle, tool, targets):
    extra = {"content": "x"} if tool == "severian_correct" else {}
    assert call(provider(bundle), tool, **targets, **extra) == {
        "error": "give exactly one of observation_id or memory_id"
    }


@pytest.mark.parametrize("context", ["cron", "subagent", "flush"])
def test_writing_tools_are_refused_outside_the_primary_context(bundle, context):
    hit = remember(bundle, "The release train leaves at 09:00")
    unattended = provider(bundle, chat="cron-1", context=context)

    attempts = [
        call(unattended, "severian_forget", memory_id=hit["memory_id"]),
        call(unattended, "severian_correct", memory_id=hit["memory_id"], content="leaves at noon"),
        call(unattended, "severian_ingest", content="injected by an unattended run"),
    ]

    assert all(attempt == {"error": "memory writes are disabled in this context"} for attempt in attempts)
    context_text = recall(bundle, "release train")
    assert "09:00" in context_text and "noon" not in context_text


def test_another_profile_cannot_forget_or_correct_these_memories(bundle):
    hit = remember(bundle, "OCTACON-ONLY fact: the build cache lives on nvme1")
    remii_scope = {**SCOPE_ARGS, "profile_id": "remii", "agent_id": "remii"}
    remii = provider(bundle, identity="remii", chat="chat-9")

    forget = call(remii, "severian_forget", scope=remii_scope, observation_id=hit["source_observation_ids"][0])
    correct = call(remii, "severian_correct", scope=remii_scope, memory_id=hit["memory_id"], content="hijacked")
    crafted = call(remii, "severian_forget", scope=None, memory_id=hit["memory_id"])  # octacon's scope args

    assert forget == {"error": "not found in scope"}
    assert correct == {"error": "not found in scope"}
    assert crafted == {"error": "scope mismatch"}
    assert "nvme1" in recall(bundle, "build cache")


def test_forgetting_twice_reports_it_is_already_gone(bundle):
    hit = remember(bundle, "The old wifi name is KenseiNet")
    first = call(provider(bundle), "severian_forget", memory_id=hit["memory_id"])
    second = call(provider(bundle), "severian_forget", memory_id=hit["memory_id"])

    assert first == {"forgotten": "memory", "id": hit["memory_id"]}
    assert second == {"error": "already corrected or forgotten"}
