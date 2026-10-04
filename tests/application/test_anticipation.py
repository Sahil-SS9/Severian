"""Anticipation (Step 4, next-improvements plan): cues, triggers, due reminders."""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from severian.application.anticipation import (
    MAX_CUES,
    MAX_REMINDERS,
    CueGenerator,
    DueContext,
    clean_cues,
    extract_trigger,
    match_due,
)
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Memory, Scope, Trigger
from severian.integrations.hermes import SeverianMemoryProvider

SCOPE = Scope(
    tenant_id="kensei", profile_id="octacon", collection_id="profile", agent_id="octacon", session_id="shared"
)

# -- triggers -------------------------------------------------------------------------------


def test_instruction_for_later_gets_a_trigger_with_path_tool_and_keywords():
    trigger = extract_trigger("Next time you touch src/auth/login.py, run `ruff` before committing")
    assert trigger is not None
    assert "src/auth/login.py" in trigger.paths
    assert "ruff" in trigger.tools
    assert trigger.keywords


def test_repo_is_picked_out():
    trigger = extract_trigger("Whenever you work in the billing repo, check the migration folder first")
    assert trigger is not None and trigger.repos == ["billing"]


@pytest.mark.parametrize(
    "text",
    ["Alice likes green tea", "The meeting is on Tuesday", "Priya flew to Lisbon", "src/auth/login.py has 300 lines"],
)
def test_plain_facts_get_no_trigger(text):
    assert extract_trigger(text) is None


def test_instruction_with_nothing_to_hang_it_on_gets_no_trigger():
    assert extract_trigger("always") is None


# -- due matching ---------------------------------------------------------------------------


def memory(text: str, trigger: Trigger | None) -> Memory:
    return Memory(
        scope=SCOPE,
        kind="fact",
        content=text,
        source_observation_ids=["o"],
        model_id="m",
        prompt_version="p",
        confidence=0.9,
        trigger=trigger,
    )


RUFF = memory("run ruff when touching login.py", Trigger(tools=["ruff"], paths=["src/auth/login.py"]))
DEPLOY = memory(
    "next time we deploy the billing service, check the invoice queue is empty",
    Trigger(keywords=["deploy", "billing", "service", "check", "invoice", "queue", "empty"]),
)
REPO = memory("in the billing repo read the migration notes", Trigger(repos=["billing"]))


def test_tool_path_and_repo_match_exactly():
    assert match_due([RUFF], DueContext(tool="RUFF")) == [RUFF]
    assert match_due([RUFF], DueContext(path="/home/k/proj/src/auth/login.py")) == [RUFF]
    assert match_due([RUFF], DueContext(path="login.py")) == [RUFF]
    assert match_due([REPO], DueContext(repo="Billing")) == [REPO]


def test_keywords_need_most_of_them_present():
    assert match_due(
        [DEPLOY], DueContext(message="we are about to deploy the billing service, check the invoice queue")
    ) == [DEPLOY]
    # one shared common word must not fire it
    assert match_due([DEPLOY], DueContext(message="please deploy the website")) == []


@pytest.mark.parametrize(
    "context",
    [
        DueContext(),
        DueContext(tool="git", path="README.md", repo="docs", message="what is the weather like"),
        DueContext(message="write a poem about the sea"),
        DueContext(path="src/auth/logout.py"),
    ],
)
def test_unrelated_context_triggers_nothing(context):
    assert match_due([RUFF, DEPLOY, REPO], context) == []


def test_never_more_than_two_reminders():
    many = [memory(f"remind {i}", Trigger(tools=["git"])) for i in range(5)]
    assert len(match_due(many, DueContext(tool="git"), limit=10)) == MAX_REMINDERS
    assert match_due(many, DueContext(tool="git"), limit=0) == []


def test_memories_without_a_trigger_never_match():
    assert match_due([memory("Alice likes tea", None)], DueContext(message="Alice likes tea")) == []


# -- cue generator --------------------------------------------------------------------------


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def answer(text: str):
    def opener(request, timeout):
        return _Response(json.dumps({"choices": [{"message": {"content": text}}]}).encode())

    return opener


def test_generator_parses_a_json_list_even_with_chatter_around_it():
    generator = CueGenerator(
        opener=answer('Sure! ["what does Alice drink", "tea or coffee for Alice"] hope that helps')
    )
    assert generator.generate("Alice likes green tea") == ["what does Alice drink", "tea or coffee for Alice"]
    assert len(generator.timings) == 1 and generator.failures == 0


def test_generator_failures_return_empty_and_are_counted():
    def broken(request, timeout):
        raise OSError("model down")

    generator = CueGenerator(opener=broken)
    assert generator.generate("anything") == []
    assert CueGenerator(opener=answer("no list here")).generate("x") == []
    assert CueGenerator(opener=answer("[1, 2, {}]")).generate("x") == []
    assert generator.failures == 1 and len(generator.timings) == 1


def test_clean_cues_trims_dedupes_and_caps():
    raw = ["  a   b ", "A B", "", 5, "x" * 500, "c", "d", "e"]
    cleaned = clean_cues(raw)
    assert cleaned[0] == "a b" and len(cleaned) == MAX_CUES and all(len(c) <= 120 for c in cleaned)
    assert clean_cues("not a list") == []


# -- through the real stores ----------------------------------------------------------------


def make_bundle(tmp_path: Path, monkeypatch, *, on: bool, cues=None, cue_weight: str | None = None) -> ServiceBundle:
    if on:
        monkeypatch.setenv("SEVERIAN_ANTICIPATION", "1")
    else:
        monkeypatch.delenv("SEVERIAN_ANTICIPATION", raising=False)
    if cue_weight:
        monkeypatch.setenv("SEVERIAN_FUSION_CUE", cue_weight)
    else:
        monkeypatch.delenv("SEVERIAN_FUSION_CUE", raising=False)
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    if cues is not None:
        built.derivation._cue_generator = cues  # the worker's derivation, as composition wires it
    return built


def say(bundle: ServiceBundle, *statements: str) -> None:
    for statement in statements:
        bundle.service.ingest_observation(SCOPE, content=statement, source={"session_id": "c"})
        assert (
            DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
            .drain(gateway=bundle.gateway)
            .failed
            == 0
        )


def stored(bundle: ServiceBundle, text: str) -> Memory:
    return next(m for m in bundle.repository.list_memories(SCOPE) if m.content == text)


def cue_rows(bundle: ServiceBundle) -> int:
    return bundle.lexical.connection.execute("SELECT COUNT(*) FROM cue_fts").fetchone()[0]


def test_switched_off_means_no_cues_no_triggers_and_nothing_due(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=False, cues=lambda text: ["never called"])
    say(bundle, "Next time you touch auth.py run ruff", "Alice likes green tea")
    for text in ("Next time you touch auth.py run ruff", "Alice likes green tea"):
        m = stored(bundle, text)
        assert m.cues == [] and m.trigger is None
    assert cue_rows(bundle) == 0
    assert bundle.service.due(SCOPE, path="auth.py") == []
    bundle.close()


def test_cues_are_stored_indexed_and_searchable_by_a_differently_worded_question(tmp_path, monkeypatch):
    cues = lambda text: ["which beverage does Alice prefer"]  # noqa: E731
    bundle = make_bundle(tmp_path, monkeypatch, on=True, cues=cues, cue_weight="1.0")
    say(bundle, "Alice likes green tea")
    assert stored(bundle, "Alice likes green tea").cues == ["which beverage does Alice prefer"]
    assert cue_rows(bundle) == 1
    hits = bundle.service.search(SCOPE, query="beverage prefer", limit=5)
    assert [h.content for h in hits] == ["Alice likes green tea"]
    # cues are never shown as facts
    assert all("beverage" not in h.content for h in hits)
    bundle.close()


def test_cue_channel_is_off_at_weight_zero(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=True, cues=lambda text: ["which beverage does Alice prefer"])
    say(bundle, "Alice likes green tea")
    searched: list[str] = []
    original = bundle.lexical.search_cues
    bundle.lexical.search_cues = lambda *a, **k: searched.append("x") or original(*a, **k)
    bundle.service.search(SCOPE, query="beverage prefer", limit=5)
    assert searched == []  # weight 0: the cue index is not even queried
    bundle.close()


def test_erasing_a_memory_erases_its_cues(tmp_path, monkeypatch):
    bundle = make_bundle(
        tmp_path, monkeypatch, on=True, cues=lambda text: ["which beverage does Alice prefer"], cue_weight="1.0"
    )
    say(bundle, "Alice likes green tea")
    assert cue_rows(bundle) == 1
    bundle.service.erase_memory(SCOPE, stored(bundle, "Alice likes green tea").id)
    assert cue_rows(bundle) == 0
    assert bundle.service.search(SCOPE, query="beverage prefer", limit=5) == []
    bundle.close()


def test_correcting_a_memory_drops_the_old_cues_and_trigger(tmp_path, monkeypatch):
    bundle = make_bundle(
        tmp_path, monkeypatch, on=True, cues=lambda text: ["which beverage does Alice prefer"], cue_weight="1.0"
    )
    say(bundle, "Alice likes green tea")
    old = stored(bundle, "Alice likes green tea")
    new = bundle.service.correct_memory(SCOPE, old.id, content="Alice likes coffee", model_id="m", prompt_version="p")
    assert new.cues == [] and new.trigger is None and cue_rows(bundle) == 0
    bundle.close()


def test_rebuilding_the_index_keeps_cues(tmp_path, monkeypatch):
    bundle = make_bundle(
        tmp_path, monkeypatch, on=True, cues=lambda text: ["which beverage does Alice prefer"], cue_weight="1.0"
    )
    say(bundle, "Alice likes green tea")
    bundle.lexical.rebuild(bundle.repository, SCOPE)
    assert cue_rows(bundle) == 1
    bundle.close()


def test_a_failing_cue_generator_never_fails_the_derive(tmp_path, monkeypatch):
    def broken(text):
        raise RuntimeError("model down")

    bundle = make_bundle(tmp_path, monkeypatch, on=True, cues=broken)
    say(bundle, "Alice likes green tea")  # say() asserts failed == 0
    assert stored(bundle, "Alice likes green tea").cues == []
    bundle.close()


def test_a_dead_model_url_still_derives(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVERIAN_CUE_URL", "http://127.0.0.1:9/v1/chat/completions")
    monkeypatch.setenv("SEVERIAN_CUE_TIMEOUT", "1")
    bundle = make_bundle(tmp_path, monkeypatch, on=True)  # the real generator, pointing nowhere
    say(bundle, "Alice likes green tea")
    assert stored(bundle, "Alice likes green tea").cues == []
    bundle.close()


def test_the_chat_path_service_never_generates_cues(tmp_path, monkeypatch):
    calls: list[str] = []
    bundle = make_bundle(tmp_path, monkeypatch, on=True, cues=lambda text: calls.append(text) or ["x"])
    bundle.service.ingest_observation(SCOPE, content="Alice likes green tea", source={"session_id": "c"})
    bundle.service.search(SCOPE, query="Alice tea", derive=True, gateway=bundle.gateway)
    assert calls == []  # derive=True on the service uses its own cue-free derivation
    bundle.close()


def test_per_memory_worker_latency_is_measured(tmp_path, monkeypatch):
    generator = CueGenerator(opener=answer('["when will Alice want a drink"]'))
    bundle = make_bundle(tmp_path, monkeypatch, on=True, cues=generator.generate)
    say(bundle, "Alice likes green tea", "Bob likes coffee")
    assert len(generator.timings) == 2 and all(t >= 0 for t in generator.timings)
    bundle.close()


def test_due_returns_matching_instructions_only_and_at_most_two(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=True)
    say(
        bundle,
        "Next time you touch src/auth/login.py, run `ruff` first",
        "Whenever you use git, check the branch name",
        "Remember to use git rebase carefully",
        "Alice likes green tea",
    )
    assert bundle.service.due(SCOPE, path="src/auth/login.py")[0].content.startswith("Next time you touch")
    assert len(bundle.service.due(SCOPE, tool="git")) == 2
    assert bundle.service.due(SCOPE, message="what is the weather", tool="curl", path="notes.txt") == []
    bundle.close()


def test_erased_instruction_is_no_longer_due(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=True)
    say(bundle, "Whenever you use git, check the branch name")
    (item,) = bundle.service.due(SCOPE, tool="git")
    bundle.service.erase_memory(SCOPE, item.memory_id)
    assert bundle.service.due(SCOPE, tool="git") == []
    bundle.close()


# -- Hermes plugin --------------------------------------------------------------------------


def provider(bundle: ServiceBundle, chat: str = "chat-9") -> SeverianMemoryProvider:
    instance = SeverianMemoryProvider(service=bundle.service, gateway=bundle.gateway)
    instance.initialize(
        chat,
        platform="discord",
        hermes_home="/hermes/profiles/octacon",
        agent_context="primary",
        agent_identity="octacon",
        agent_workspace="hermes",
    )
    return instance


def test_prefetch_adds_a_reminder_once_per_session_and_logs_it(tmp_path, monkeypatch, caplog):
    bundle = make_bundle(tmp_path, monkeypatch, on=True)
    say(bundle, "Whenever you use git, check the branch name")
    agent = provider(bundle)
    with caplog.at_level(logging.INFO, logger="severian.integrations.hermes"):
        first = agent.prefetch("commit my changes", tool="git", session_id="chat-9")
        second = agent.prefetch("commit my changes", tool="git", session_id="chat-9")
    assert "check the branch name" in first and "Reminders" in first
    assert "Reminders" not in second
    assert sum("reminder shown" in r.message for r in caplog.records) == 1
    # a new session may be reminded again
    assert "check the branch name" in provider(bundle, chat="chat-10").prefetch(
        "commit", tool="git", session_id="chat-10"
    )
    bundle.close()


def test_prefetch_has_no_reminders_when_off(tmp_path, monkeypatch):
    off = make_bundle(tmp_path, monkeypatch, on=False)
    say(off, "Whenever you use git, check the branch name")
    assert "Reminders" not in provider(off).prefetch("commit", tool="git", session_id="chat-9")
    off.close()


def test_prefetch_has_no_reminders_for_unrelated_work(tmp_path, monkeypatch):
    on = make_bundle(tmp_path, monkeypatch, on=True)
    say(on, "Whenever you use git, check the branch name")
    assert "Reminders" not in provider(on).prefetch("what is the weather", tool="curl", session_id="chat-9")
    on.close()


def test_cue_index_finds_the_memory_by_cue_words_alone(tmp_path, monkeypatch):
    bundle = make_bundle(
        tmp_path,
        monkeypatch,
        on=True,
        cues=lambda text: ["which beverage does Alice prefer"] if "Alice" in text else [],
    )
    say(bundle, "Alice likes green tea", "Bob watched a football match")
    target = stored(bundle, "Alice likes green tea")
    assert [h.memory_id for h in bundle.lexical.search_cues(SCOPE, "beverage")] == [target.id]
    assert bundle.lexical.search_cues(SCOPE, "football") == []  # words in the memory text are not cue words
    bundle.close()


def test_cue_weight_raises_the_score_of_the_cued_memory(tmp_path, monkeypatch):
    scores = {}
    for weight in ("0", "1.0"):
        folder = tmp_path / weight
        folder.mkdir()
        bundle = make_bundle(
            folder,
            monkeypatch,
            on=True,
            cues=lambda text: ["which beverage does Alice prefer"] if "Alice" in text else [],
            cue_weight=weight,
        )
        say(bundle, "Alice likes green tea", "Bob watched a football match")
        results = bundle.service._retrieval.search(SCOPE, "beverage", limit=5)
        scores[weight] = next((r.fused_score for r in results if r.memory.content == "Alice likes green tea"), 0.0)
        bundle.close()
    assert scores["1.0"] > scores["0"]
