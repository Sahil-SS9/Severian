"""Task notes: "where did we leave off" for agents moving off Mnemosyne (``severian_task_progress``).

One current note per task key, per scope. Setting it again replaces the note and keeps the old one as
history; clearing forgets it. Notes are visible to the same profile in any chat, never to another
profile, and unattended runs (cron, subagent, flush) can read but not write them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from severian.composition import ServiceBundle, build_bundle
from severian.integrations.hermes import SeverianMemoryProvider

SCOPE = {"tenant_id": "kensei", "profile_id": "octacon", "collection_id": "profile",
         "agent_id": "octacon", "session_id": "shared"}


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def provider(bundle: ServiceBundle, identity: str = "octacon", chat: str = "chat-1", context: str = "primary"):
    instance = SeverianMemoryProvider(service=bundle.service, gateway=bundle.gateway)
    instance.initialize(chat, platform="discord", hermes_home=f"/hermes/profiles/{identity}",
                        agent_context=context, agent_identity=identity, agent_workspace="hermes")
    return instance


def tp(instance, scope=None, **args) -> dict:
    return json.loads(instance.handle_tool_call("severian_task_progress", {**(scope or SCOPE), **args}))


def test_tool_is_offered_with_scope():
    (schema,) = [s for s in SeverianMemoryProvider().get_tool_schemas() if s["name"] == "severian_task_progress"]
    assert set(SCOPE) | {"action"} <= set(schema["parameters"]["required"])
    assert schema["parameters"]["properties"]["action"]["enum"] == ["set", "get", "list", "clear"]


def test_set_then_get_in_another_chat(bundle):
    assert tp(provider(bundle), action="set", task="hps", state="batch 3 merged; next: audit")["task"] == "hps"
    got = tp(provider(bundle, chat="chat-2"), action="get", task="hps")
    assert got["state"] == "batch 3 merged; next: audit"
    assert got["updated_at"]


def test_set_again_replaces_and_keeps_history(bundle):
    p = provider(bundle)
    tp(p, action="set", task="hps", state="first")
    tp(p, action="set", task="hps", state="second")
    got = tp(p, action="get", task="hps", include_history=True)
    assert got["state"] == "second"
    assert [h["state"] for h in got["history"]] == ["first"]
    assert [n["task"] for n in tp(p, action="list")["tasks"]] == ["hps"]


def test_metadata_round_trips(bundle):
    p = provider(bundle)
    tp(p, action="set", task="hps", state="s", metadata={"next_step": "audit", "blockers": []})
    assert tp(p, action="get", task="hps")["metadata"] == {"next_step": "audit", "blockers": []}


def test_list_is_newest_first_and_clear_forgets(bundle):
    p = provider(bundle)
    tp(p, action="set", task="a", state="1")
    tp(p, action="set", task="b", state="2")
    assert [n["task"] for n in tp(p, action="list")["tasks"]] == ["b", "a"]
    assert tp(p, action="clear", task="a") == {"cleared": "a"}
    assert [n["task"] for n in tp(p, action="list")["tasks"]] == ["b"]
    assert tp(p, action="get", task="a") == {"error": "no note for this task"}


def test_other_profile_cannot_see_or_reach_it(bundle):
    tp(provider(bundle), action="set", task="hps", state="secret")
    remii = provider(bundle, identity="remii")
    other = {**SCOPE, "profile_id": "remii", "agent_id": "remii"}
    assert tp(remii, scope=other, action="list")["tasks"] == []
    assert "error" in tp(remii, action="get", task="hps")  # crafted scope is refused


def test_unattended_runs_read_but_do_not_write(bundle):
    tp(provider(bundle), action="set", task="hps", state="kept")
    cron = provider(bundle, context="cron")
    assert tp(cron, action="get", task="hps")["state"] == "kept"
    assert "disabled" in tp(cron, action="set", task="hps", state="overwrite")["error"]
    assert "disabled" in tp(cron, action="clear", task="hps")["error"]


def test_notes_do_not_show_as_search_facts(bundle):
    p = provider(bundle)
    tp(p, action="set", task="hps", state="zebra crossing plan")
    hits = json.loads(p.handle_tool_call("severian_search", {**SCOPE, "query": "zebra crossing plan"}))["hits"]
    assert hits == []


@pytest.mark.parametrize("args", [
    {"action": "set", "task": "x"},
    {"action": "set", "state": "y"},
    {"action": "get"},
    {"action": "bogus", "task": "x"},
    {"action": "set", "task": "x", "state": "y", "metadata": "nope"},
    {"action": "list", "extra": 1},
])
def test_bad_arguments_are_refused(bundle, args):
    assert "error" in tp(provider(bundle), **args)

