"""Round 2, idea 1: facts the user stated, extracted in the background (SEVERIAN_USER_FACTS, off).

Saving stays as fast as today: the normal derive pass makes the raw turn searchable and never
calls the model. A separate background pass (``DerivationWorker.extract_facts``) sends batches of
consecutive user messages to the local model and stores each fact as a ``user_fact`` memory that
keeps the raw turn linked (same source observation).

Borrowed (see docs/severian-programme/ROUND2-IDEA1-*.md): the LongMemEval paper's user-fact prompt
and per-session batching (src/index_expansion/batch_expansion_session_userfact.py); Mem0's rule
that facts come only from the user's messages (mem0/configs/prompts.py); Hindsight's
self-contained facts, pronouns replaced (hindsight_api/engine/retain/fact_extraction.py).
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError

import pytest
from sqlalchemy import event, select, update
from typer.testing import CliRunner

from severian.application.eventtime import extract_event_span, message_time
from severian.application.userfacts import (
    FACTS_JOB,
    JSON_ONLY,
    MAX_FACT_CHARS,
    MAX_FACTS_PER_MESSAGE,
    MAX_KEPT_REPLIES,
    MAX_KEPT_REPLY_CHARS,
    RETRY_PAUSE_S,
    Extraction,
    FactExtractor,
    clean_facts,
    facts_skip_profiles,
    user_text,
)
from severian.application.worker import FACTS_MAX_ATTEMPTS, FACTS_RETRY_BASE_S, DerivationWorker, WorkerSummary
from severian.cli.app import cli
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Job, Memory, Scope
from severian.infrastructure.sqlite import records

SCOPE = Scope(tenant_id="kensei", profile_id="sahil", collection_id="profile", agent_id="kensei", session_id="shared")
LABEL = "[2023/05/20 (Sat) 10:00]"

# -- what the user said ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "source", "expected"),
    [
        (f"{LABEL} user: I moved to York\nassistant: Nice!", {}, "I moved to York"),
        ("user: I moved to York", {}, "I moved to York"),
        ("user: line one\nline two\nassistant: ok", {}, "line one\nline two"),
        (f"{LABEL} assistant: Hello there", {"turn": 1}, None),  # nothing the user said
        ("I moved to York", {"turn": 3}, "I moved to York"),  # a Hermes turn with no reply yet
        (f"{LABEL} I moved to York", {"turn": 3}, "I moved to York"),
        ("Here is a recipe for you", {"turn": 2, "speaker": "assistant"}, None),  # a bare assistant message
        ("delegated task: x\ndelegated result: y", {}, None),  # not a conversation turn
        ("user:    \nassistant: hi", {}, None),
    ],
)
def test_user_text_is_only_what_the_user_said(content, source, expected):
    assert user_text(content, source) == expected


@pytest.mark.parametrize(
    ("content", "source"),
    [
        ("user: work kanban task t_af102605\nassistant: Task done.", {"platform": "cli"}),  # a kanban worker's dispatch
        ("user: Summarise the overnight runs\nassistant: ok", {"platform": "cron"}),
        ("user: Pick up the next card\nassistant: ok", {"platform": "kanban"}),
    ],
)
def test_machine_prompts_are_not_the_user_speaking(content, source):
    """16/10: facts like "The user is working on kanban task t_af102605" came from bot-to-bot prompts."""
    assert user_text(content, source) is None


def test_a_person_mentioning_kanban_is_still_the_user():
    said = "I want to work kanban task t_af102605 myself tomorrow"
    assert user_text(f"user: {said}\nassistant: ok", {"platform": "discord"}) == said


def test_profiles_can_be_left_out_of_fact_extraction():
    assert facts_skip_profiles({}) == frozenset()
    assert facts_skip_profiles({"SEVERIAN_FACTS_SKIP_PROFILES": " custom, eval-bench ,"}) == {"custom", "eval-bench"}


def test_clean_facts_accepts_both_reply_shapes_dedupes_and_caps():
    reply = {"1": ["The user moved to York.", " the user  moved to York. "], "2": [], "x": ["bad"], "9": ["out"]}
    assert clean_facts(reply, count=2) == {1: ["The user moved to York."]}
    as_list = [{"msg": 2, "fact": "The user has a dog."}, {"msg": "1", "fact": "The user likes tea."},
               {"fact": "no message number"}, "junk"]
    assert clean_facts(as_list, count=2) == {1: ["The user likes tea."], 2: ["The user has a dog."]}
    many = {"1": [f"The user owns item {i}." for i in range(20)]}
    assert len(clean_facts(many, count=1)[1]) == MAX_FACTS_PER_MESSAGE
    assert all(len(f) <= MAX_FACT_CHARS for f in clean_facts({"1": ["x" * 900]}, count=1)[1])
    assert clean_facts("not a mapping", count=1) == {}


# -- the model call -------------------------------------------------------------------------


class _Reply:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def model(reply: str, seen: list | None = None, usage: dict | None = None):
    def opener(request, timeout):
        if seen is not None:
            seen.append(json.loads(request.data))
        return _Reply({"choices": [{"message": {"content": reply}}],
                       "usage": usage or {"prompt_tokens": 50, "completion_tokens": 20}})

    return opener


def no_pause(_seconds: float) -> None:
    return None


@pytest.mark.parametrize(
    "reply",
    [
        '{"1": ["The user moved to York."], "2": []}',
        '<think>\n{"1": ["not this"]}\n</think>\n\n{"1": ["The user moved to York."]}',
        '```json\n{"1": ["The user moved to York."]}\n```\nDone.',
        '[{"msg": 1, "fact": "The user moved to York."}]',
        "{'1': ['The user moved to York.']}",
    ],
)
def test_extractor_parses_common_reply_shapes(reply):
    extractor = FactExtractor(opener=model(reply))
    assert extractor.extract(["I moved to York", "thanks"]) == Extraction(facts={1: ["The user moved to York."]})
    assert extractor.failures == 0 and extractor.calls == 1


def test_extractor_numbers_messages_in_order_and_records_cost():
    seen: list = []
    extractor = FactExtractor(opener=model("{}", seen, usage={"prompt_tokens": 120, "completion_tokens": 7}))
    assert extractor.extract(["first thing", "second thing"]) == Extraction()
    sent = seen[0]
    assert sent["model"] == "active:aux" and sent["temperature"] == 0
    text = sent["messages"][-1]["content"]
    assert text.index("[1] first thing") < text.index("[2] second thing")
    assert (extractor.prompt_tokens, extractor.completion_tokens, len(extractor.timings)) == (120, 7, 1)
    assert (extractor.batches, extractor.first_failures, extractor.failed_batches) == (1, 0, 0)


def test_extractor_cuts_long_messages_before_sending():
    seen: list = []
    FactExtractor(opener=model("{}", seen)).extract(["y" * 10_000])
    assert len(seen[0]["messages"][-1]["content"]) < 2_500


def echo_model(seen: list | None = None, *, unreadable: str = "BAD"):
    """States one fact per message, but answers in prose whenever the batch holds ``unreadable``."""

    def opener(request, timeout):
        sent = json.loads(request.data)
        if seen is not None:
            seen.append(sent)
        said = re.findall(r"^\[(\d+)\] (.*)$", sent["messages"][-1]["content"], flags=re.MULTILINE)
        if any(unreadable in text for _number, text in said):
            content = "Sorry, I can't help with that."
        else:
            content = json.dumps({number: [f"The user said {text}."] for number, text in said})
        return _Reply({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    return opener


def batch_sizes(seen: list) -> list[int]:
    return [len(re.findall(r"^\[\d+\] ", sent["messages"][-1]["content"], flags=re.MULTILINE)) for sent in seen]


def test_a_bad_reply_is_kept_and_the_batch_retried_once():
    replies = iter(["Sorry, I can't help with that.", '{"1": ["The user moved to York."]}'])

    def opener(request, timeout):
        return _Reply({"choices": [{"message": {"content": next(replies)}, "finish_reason": "stop"}]})

    extractor = FactExtractor(opener=opener, sleep=no_pause)
    assert extractor.extract(["I moved to York"]) == Extraction(facts={1: ["The user moved to York."]})
    assert (extractor.calls, extractor.failures, extractor.first_failures, extractor.retries) == (2, 1, 1, 1)
    assert (extractor.failed_batches, extractor.given_up) == (0, 0)
    assert extractor.bad_replies == [
        {"reason": "no JSON object or list in the reply", "reply": "Sorry, I can't help with that.", "messages": 1}
    ]


def test_the_retry_after_an_unreadable_reply_asks_for_json_only():
    """At temperature 0 an identical retry repeats the bad reply (seen on the real model, 05/10)."""
    seen: list = []

    def opener(request, timeout):
        sent = json.loads(request.data)
        seen.append(sent)
        asked = sent["messages"][-1]["content"]
        content = '{"1": ["The user moved to York."]}' if JSON_ONLY in asked else "[1] The user moved to York."
        return _Reply({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    extractor = FactExtractor(opener=opener, sleep=no_pause)
    assert extractor.extract(["I moved to York"]) == Extraction(facts={1: ["The user moved to York."]})
    assert JSON_ONLY not in seen[0]["messages"][-1]["content"] and extractor.splits == 0


def test_a_batch_that_fails_twice_is_split_until_the_unreadable_message_is_alone():
    seen: list = []
    extractor = FactExtractor(opener=echo_model(seen), sleep=no_pause)
    got = extractor.extract(["one", "two", "BAD three", "four"])
    assert got.facts == {1: ["The user said one."], 2: ["The user said two."], 4: ["The user said four."]}
    assert list(got.failed) == [3]
    assert got.failed[3].startswith("no JSON object or list in the reply") and "Sorry, I can't" in got.failed[3]
    # all four: first try + one retry; then halves, and a half that fails is split again (no more retries)
    assert batch_sizes(seen) == [4, 4, 2, 2, 1, 1]
    assert (extractor.batches, extractor.first_failures, extractor.retries, extractor.splits) == (1, 1, 1, 2)
    assert (extractor.calls, extractor.failures, extractor.failed_batches, extractor.given_up) == (6, 4, 1, 1)
    assert len(extractor.bad_replies) == 4


def unavailable(calls: list, body: bytes = b"loading model"):
    def opener(request, timeout):
        calls.append(json.loads(request.data))
        raise HTTPError("http://127.0.0.1:8091", 503, "Service Unavailable", hdrs=None, fp=io.BytesIO(body))  # type: ignore[arg-type]

    return opener


def test_a_server_fault_is_retried_once_after_a_pause_and_never_split():
    calls: list = []
    pauses: list[float] = []
    extractor = FactExtractor(opener=unavailable(calls), sleep=pauses.append)
    got = extractor.extract(["a", "b", "c", "d"])
    assert got.facts == {} and sorted(got.failed) == [1, 2, 3, 4]
    assert "HTTP Error 503" in got.failed[1] and "loading model" in got.failed[1]
    assert len(calls) == 2 and pauses == [RETRY_PAUSE_S] and extractor.splits == 0
    assert (extractor.failed_batches, extractor.given_up) == (1, 4)

    first: list = []
    down_once = unavailable(first, b"")

    def flaky(request, timeout):
        if not first:
            return down_once(request, timeout)
        tea = '{"1": ["The user likes tea."]}'
        return _Reply({"choices": [{"message": {"content": tea}, "finish_reason": "stop"}]})

    recovered = FactExtractor(opener=flaky, sleep=no_pause)
    assert recovered.extract(["I like tea"]) == Extraction(facts={1: ["The user likes tea."]})


def busy(calls: list):
    """The model gateway's answer while an identical request is still being served (16/10: Turbofit)."""
    body = b'{"error": "request_in_progress", "message": "An identical request is already running", "retryable": true}'

    def opener(request, timeout):
        calls.append(json.loads(request.data))
        raise HTTPError("http://127.0.0.1:8091", 409, "Conflict", hdrs=None, fp=io.BytesIO(body))  # type: ignore[arg-type]

    return opener


def test_a_busy_model_is_a_server_fault_retried_once_and_never_split():
    calls: list = []
    pauses: list[float] = []
    extractor = FactExtractor(opener=busy(calls), sleep=pauses.append)
    got = extractor.extract(["a", "b", "c"])
    assert got.facts == {} and sorted(got.failed) == [1, 2, 3] and "request_in_progress" in got.failed[1]
    assert len(calls) == 2 and pauses == [RETRY_PAUSE_S] and extractor.splits == 0


def test_server_faults_are_marked_retryable_and_unreadable_replies_are_not():
    assert FactExtractor(opener=unavailable([]), sleep=no_pause).extract(["a", "b"]).retryable == frozenset({1, 2})
    assert FactExtractor(opener=busy([]), sleep=no_pause).extract(["a"]).retryable == frozenset({1})

    def timed_out(request, timeout):
        raise TimeoutError("timed out")

    assert FactExtractor(opener=timed_out, sleep=no_pause).extract(["a"]).retryable == frozenset({1})
    unreadable = FactExtractor(opener=echo_model(), sleep=no_pause).extract(["one", "BAD two"])
    assert list(unreadable.failed) == [2] and unreadable.retryable == frozenset()


def test_a_reply_cut_off_at_the_token_limit_is_retried_once_with_more_room():
    seen: list = []
    replies = iter([('{"1": ["The user', "length"), ('{"1": ["The user moved to York."]}', "stop")])

    def opener(request, timeout):
        seen.append(json.loads(request.data))
        content, finish = next(replies)
        return _Reply({"choices": [{"message": {"content": content}, "finish_reason": finish}]})

    extractor = FactExtractor(opener=opener, sleep=no_pause)
    assert extractor.extract(["I moved to York"]) == Extraction(facts={1: ["The user moved to York."]})
    assert seen[1]["max_tokens"] == 2 * seen[0]["max_tokens"]


def test_a_message_that_keeps_failing_is_given_up_and_logged_once(caplog):
    def broken(request, timeout):
        raise OSError("connection refused")

    extractor = FactExtractor(opener=broken, sleep=no_pause)
    with caplog.at_level(logging.WARNING, logger="severian.application.userfacts"):
        for _ in range(3):
            got = extractor.extract(["I moved to York"])
            assert got.facts == {} and "connection refused" in got.failed[1]
    assert (extractor.calls, extractor.failures, extractor.given_up, extractor.failed_batches) == (6, 6, 3, 3)
    assert len([r for r in caplog.records if "fact extraction failed" in r.getMessage()]) == extractor.failures

    def cut_off(request, timeout):
        return _Reply({"choices": [{"message": {"content": '{"1": ["The user'}, "finish_reason": "length"}]})

    stuck = FactExtractor(opener=cut_off, sleep=no_pause)
    assert stuck.extract(["I moved to York"]).failed[1].startswith("reply cut off at max_tokens")
    assert stuck.calls == 2


def test_kept_replies_are_bounded():
    extractor = FactExtractor(opener=model("x" * (MAX_KEPT_REPLY_CHARS + 500)), sleep=no_pause)
    for _ in range(MAX_KEPT_REPLIES):
        extractor.extract(["I moved to York"])
    assert extractor.failures == 2 * MAX_KEPT_REPLIES and len(extractor.bad_replies) == MAX_KEPT_REPLIES
    assert all(len(kept["reply"]) == MAX_KEPT_REPLY_CHARS for kept in extractor.bad_replies)


def test_extractor_settings_come_from_the_environment():
    assert FactExtractor.from_env({}).model == "active:aux"
    custom = FactExtractor.from_env(
        {"SEVERIAN_FACTS_MODEL": "m", "SEVERIAN_FACTS_URL": "http://x/v1", "SEVERIAN_FACTS_TIMEOUT": "5"}
    )
    assert (custom.model, custom.url, custom.timeout) == ("m", "http://x/v1", 5.0)


def test_nothing_to_send_means_no_call():
    extractor = FactExtractor(opener=model("{}"))
    assert extractor.extract([]) == Extraction() and extractor.calls == 0 and extractor.batches == 0


# -- through the real stores ----------------------------------------------------------------


class FakeExtractor:
    model = "fake-model"

    def __init__(self, facts_for=lambda text: [], *, unreadable: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self._facts_for = facts_for
        self._unreadable = unreadable

    def extract(self, messages: list[str]) -> Extraction:
        self.calls.append(list(messages))
        failed = {i: "unreadable reply" for i, text in enumerate(messages, 1)
                  if self._unreadable is not None and self._unreadable in text}
        found = {i: facts for i, text in enumerate(messages, 1) if i not in failed and (facts := self._facts_for(text))}
        return Extraction(facts=found, failed=failed)


def make_bundle(tmp_path: Path, monkeypatch, *, on: bool, extractor=None) -> ServiceBundle:
    if on:
        monkeypatch.setenv("SEVERIAN_USER_FACTS", "1")
    else:
        monkeypatch.delenv("SEVERIAN_USER_FACTS", raising=False)
    tmp_path.mkdir(parents=True, exist_ok=True)
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    if extractor is not None:
        built.derivation._fact_extractor = extractor  # the worker's derivation, as composition wires it
    return built


def test_composition_wires_the_fact_model_only_when_the_switch_is_on(tmp_path, monkeypatch):
    off = make_bundle(tmp_path / "off", monkeypatch, on=False)
    assert off.derivation.fact_extractor is None
    off.close()
    monkeypatch.setenv("SEVERIAN_FACTS_MODEL", "some-model")
    on = make_bundle(tmp_path / "on", monkeypatch, on=True)
    assert isinstance(on.derivation.fact_extractor, FactExtractor)
    assert on.derivation.fact_extractor.model == "some-model"
    on.close()


def say(bundle: ServiceBundle, *turns: tuple[str, str], scope: Scope = SCOPE) -> None:
    for number, (user, assistant) in enumerate(turns, 1):
        bundle.service.ingest_observation(
            scope, content=f"{LABEL} user: {user}\nassistant: {assistant}",
            source={"session_id": "s1", "turn": number, "timestamp": "2023-05-20T10:00:00+00:00"},
        )


def worker(bundle: ServiceBundle) -> DerivationWorker:
    return DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)


def drain(bundle: ServiceBundle) -> None:
    summary = worker(bundle).drain(gateway=bundle.gateway)
    assert summary.failed == 0 and summary.projection_failed == 0


def extract(bundle: ServiceBundle, **kwargs) -> WorkerSummary:
    summary = worker(bundle).extract_facts(**kwargs)
    assert summary.failed == 0 and summary.projection_failed == 0
    return summary


def facts(bundle: ServiceBundle, scope: Scope = SCOPE) -> list[Memory]:
    return [m for m in bundle.repository.list_memories(scope) if m.kind == "user_fact"]


def fact_jobs(bundle: ServiceBundle) -> list[Job]:
    with bundle.repository.engine.connect() as connection:  # type: ignore[attr-defined]
        rows = connection.execute(select(records.c.payload).where(records.c.record_type == "job")).scalars()
        return [job for job in map(Job.model_validate, rows) if job.kind == FACTS_JOB]


def found(bundle: ServiceBundle, query: str) -> list[str]:
    return [hit.content for hit in bundle.service.search(SCOPE, query=query, limit=5)]


def test_switched_off_queues_nothing_and_never_calls_the_model(tmp_path, monkeypatch):
    fake = FakeExtractor(lambda text: ["The user lives in York."])
    bundle = make_bundle(tmp_path, monkeypatch, on=False, extractor=fake)
    say(bundle, ("I moved to York", "Nice!"))
    drain(bundle)
    assert extract(bundle).processed == 0
    assert fake.calls == [] and facts(bundle) == []
    bundle.close()


def test_saving_stays_fast_and_facts_arrive_in_the_background(tmp_path, monkeypatch):
    fake = FakeExtractor(lambda text: ["The user lives in York."] if "York" in text else [])
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to York last spring", "Nice! How is it?"))
    drain(bundle)  # the normal pass: the raw turn is saved and searchable, no model call
    assert fake.calls == [] and facts(bundle) == []
    assert any("I moved to York last spring" in hit for hit in found(bundle, "York"))

    assert extract(bundle).processed == 1
    assert fake.calls == [["I moved to York last spring"]]  # only what the user said
    [fact] = facts(bundle)
    assert fact.content == f"{LABEL} The user lives in York."  # same time label as its turn
    raw = next(m for m in bundle.repository.list_memories(SCOPE) if m.kind != "user_fact")
    assert fact.source_observation_ids == raw.source_observation_ids  # the raw turn stays linked
    assert (fact.model_id, fact.prompt_version, fact.cues, fact.trigger) == ("fake-model", "user-facts-v2", [], None)
    assert fact.content in found(bundle, "where does the user live")
    assert extract(bundle).processed == 0  # each turn is done once
    bundle.close()


def test_the_normal_pass_never_takes_extraction_jobs(tmp_path, monkeypatch):
    fake = FakeExtractor(lambda text: ["The user lives in York."])
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to York", "ok"), ("I like tea", "ok"))
    for _ in range(3):
        drain(bundle)
    assert fake.calls == [] and extract(bundle).processed == 2
    bundle.close()


def test_consecutive_turns_go_to_the_model_together_in_order(tmp_path, monkeypatch):
    fake = FakeExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, *[(f"message number {i}", "ok") for i in range(1, 11)])
    drain(bundle)
    assert extract(bundle, batch_size=4).processed == 10
    numbers = [[int(text.split()[-1]) for text in call] for call in fake.calls]
    assert numbers == [[1, 2, 3, 4], [5, 6, 7, 8], [9, 10]]
    bundle.close()


def test_max_batches_bounds_one_pass(tmp_path, monkeypatch):
    fake = FakeExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, *[(f"message number {i}", "ok") for i in range(1, 6)])
    drain(bundle)
    assert extract(bundle, batch_size=2, max_batches=1).processed == 2
    assert extract(bundle, batch_size=2).processed == 3
    bundle.close()


def test_a_batch_never_mixes_two_users(tmp_path, monkeypatch):
    fake = FakeExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    other = SCOPE.model_copy(update={"profile_id": "someone-else", "agent_id": "someone-else"})
    say(bundle, ("mine one", "ok"))
    say(bundle, ("theirs", "ok"), scope=other)
    bundle.service.ingest_observation(SCOPE, content="user: mine two\nassistant: ok", source={"turn": 2})
    drain(bundle)
    assert extract(bundle, batch_size=8).processed == 3
    assert sorted(fake.calls) == [["mine one", "mine two"], ["theirs"]]
    bundle.close()


def test_turns_with_nothing_from_the_user_are_not_sent(tmp_path, monkeypatch):
    fake = FakeExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    bundle.service.ingest_observation(SCOPE, content="assistant: Welcome back!", source={"turn": 1})
    drain(bundle)
    assert extract(bundle).processed == 1 and fake.calls == []
    bundle.close()


def test_a_failing_model_loses_only_the_facts(tmp_path, monkeypatch):
    class Broken(FakeExtractor):
        def extract(self, messages):
            self.calls.append(list(messages))
            raise RuntimeError("model down")

    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=Broken())
    say(bundle, ("I moved to York", "ok"))
    drain(bundle)
    summary = extract(bundle)  # asserts failed == 0: storage is fine, only the facts are missing
    assert (summary.processed, summary.gave_up, summary.total) == (0, 1, 1) and facts(bundle) == []
    [job] = fact_jobs(bundle)
    assert job.status == "failed" and "model down" in (job.last_error or "")  # kept, and never retried in a loop
    assert extract(bundle).total == 0
    assert any("I moved to York" in hit for hit in found(bundle, "York"))
    bundle.close()


def test_turns_the_model_cannot_read_fail_with_the_reason_and_the_rest_keep_their_facts(tmp_path, monkeypatch):
    fake = FakeExtractor(lambda text: [f"The user said {text}."], unreadable="BAD")
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to York", "ok"), ("BAD news about the boiler", "ok"))
    drain(bundle)
    summary = extract(bundle)
    assert (summary.processed, summary.gave_up, summary.failed) == (1, 1, 0)
    assert [f.content for f in facts(bundle)] == [f"{LABEL} The user said I moved to York."]
    by_status = {job.status: job for job in fact_jobs(bundle)}
    assert sorted(by_status) == ["done", "failed"] and by_status["failed"].last_error == "unreadable reply"
    assert any("boiler" in hit for hit in found(bundle, "boiler"))  # the turn itself stays searchable
    bundle.close()


class BusyExtractor(FakeExtractor):
    """The model server is busy or down for every call: each turn comes back failed AND retryable."""

    def extract(self, messages: list[str]) -> Extraction:
        self.calls.append(list(messages))
        numbers = range(1, len(messages) + 1)
        return Extraction(failed={i: "timed out" for i in numbers}, retryable=frozenset(numbers))


def _due_now(bundle: ServiceBundle) -> None:
    """Move every deferred job's retry time into the past (stands in for waiting out the backoff)."""
    with bundle.repository.engine.begin() as connection:  # type: ignore[attr-defined]
        connection.execute(update(records).where(records.c.record_type == "job").values(retry_after=None))


def test_a_busy_model_defers_the_turn_with_backoff_instead_of_failing_it(tmp_path, monkeypatch):
    fake = BusyExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to York", "ok"))
    drain(bundle)
    summary = extract(bundle)
    assert (summary.processed, summary.gave_up, summary.deferred) == (0, 0, 1)
    [job] = fact_jobs(bundle)
    assert job.status == "pending" and job.last_error == "timed out" and job.attempts == 1
    soonest = datetime.now(UTC) + timedelta(seconds=FACTS_RETRY_BASE_S - 5)
    assert job.retry_after is not None and job.retry_after > soonest
    assert extract(bundle).total == 0 and len(fake.calls) == 1  # not retried before its time

    _due_now(bundle)
    assert extract(bundle).deferred == 1 and len(fake.calls) == 2
    [job] = fact_jobs(bundle)
    first_wait = FACTS_RETRY_BASE_S
    assert job.retry_after is not None and job.retry_after > datetime.now(UTC) + timedelta(seconds=2 * first_wait - 5)
    bundle.close()


def test_a_turn_still_unserved_after_the_attempt_limit_fails_with_its_reason(tmp_path, monkeypatch):
    fake = BusyExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to York", "ok"))
    drain(bundle)
    for _ in range(FACTS_MAX_ATTEMPTS - 1):
        assert extract(bundle).deferred == 1
        _due_now(bundle)
    summary = extract(bundle)
    assert (summary.deferred, summary.gave_up) == (0, 1) and len(fake.calls) == FACTS_MAX_ATTEMPTS
    [job] = fact_jobs(bundle)
    assert job.status == "failed" and job.last_error == "timed out" and job.attempts == FACTS_MAX_ATTEMPTS
    _due_now(bundle)
    assert extract(bundle).total == 0  # a failed job is never picked up again
    bundle.close()


def test_a_skipped_profile_never_reaches_the_model(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVERIAN_FACTS_SKIP_PROFILES", "eval-bench")
    fake = FakeExtractor(lambda text: ["The user lives in York."])
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    bench = SCOPE.model_copy(update={"profile_id": "eval-bench", "agent_id": "eval-bench"})
    say(bundle, ("I moved to York", "ok"), scope=bench)
    say(bundle, ("I moved to York", "ok"))
    drain(bundle)
    assert extract(bundle).processed == 2
    assert fake.calls == [["I moved to York"]]  # only the real profile's turn was sent
    assert facts(bundle, bench) == [] and len(facts(bundle)) == 1
    bundle.close()


def test_a_busy_model_recovering_later_still_gets_the_facts(tmp_path, monkeypatch):
    fake = BusyExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to York", "ok"))
    drain(bundle)
    assert extract(bundle).deferred == 1
    bundle.derivation._fact_extractor = FakeExtractor(lambda text: ["The user lives in York."])
    _due_now(bundle)
    assert extract(bundle).processed == 1
    assert [f.content for f in facts(bundle)] == [f"{LABEL} The user lives in York."]
    assert [job.status for job in fact_jobs(bundle)] == ["done"]
    bundle.close()


def test_correcting_a_turn_replaces_its_facts(tmp_path, monkeypatch):
    fake = FakeExtractor(lambda text: [f"The user lives in {text.split()[-1]}."])
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I live in Leeds", "ok"))
    drain(bundle)
    extract(bundle)
    assert [f.content for f in facts(bundle)] == [f"{LABEL} The user lives in Leeds."]
    [turn] = bundle.repository.list_observations(SCOPE)
    bundle.service.revise_observation(SCOPE, turn.id, f"{LABEL} user: I live in York\nassistant: ok")
    drain(bundle)
    extract(bundle)
    assert [f.content for f in facts(bundle)] == [f"{LABEL} The user lives in York."]
    assert not any("Leeds" in hit for hit in found(bundle, "where does the user live"))
    bundle.close()


def test_a_turn_corrected_before_extraction_is_never_sent(tmp_path, monkeypatch):
    fake = FakeExtractor()
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I live in Leeds", "ok"))
    drain(bundle)
    [turn] = bundle.repository.list_observations(SCOPE)
    bundle.service.revise_observation(SCOPE, turn.id, f"{LABEL} user: I live in York\nassistant: ok")
    drain(bundle)
    extract(bundle)
    assert fake.calls == [["I live in York"]]
    bundle.close()


def test_erasing_a_turn_erases_its_facts(tmp_path, monkeypatch):
    fake = FakeExtractor(lambda text: ["The user lives in York."])
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to York", "ok"))
    drain(bundle)
    extract(bundle)
    [turn] = bundle.repository.list_observations(SCOPE)
    bundle.service.erase_observation(SCOPE, turn.id)
    assert facts(bundle) == [] and not any("York" in hit for hit in found(bundle, "York"))
    bundle.close()


def test_facts_get_event_time_when_that_switch_is_on(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVERIAN_EVENT_TIME", "1")
    fake = FakeExtractor(lambda text: ["The user went to Paris yesterday."])
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I went to Paris yesterday", "ok"))
    drain(bundle)
    extract(bundle)
    [fact] = facts(bundle)
    [turn] = bundle.repository.list_observations(SCOPE)
    span = extract_event_span(fact.content, message_time(turn.source, turn.created_at))
    assert span is not None and (fact.occurred_at, fact.occurred_until) == span
    bundle.close()


def test_the_extraction_job_kind_is_stable():
    assert FACTS_JOB == "extract_facts"  # stored in job rows; renaming it strands queued work


def test_facts_not_grounded_in_their_own_message_are_never_stored(tmp_path, monkeypatch):
    """Misattached names/places (a neighbouring message's Glasgow) and facts from pasted text are dropped."""
    transcript = "Transcript 4: Dolly\nQ. Hello?\nA. I'm from New Delhi.\nQ. Clothes?\nA. Baggy jeans."
    replies = {
        "I'm living in Cardiff now.": ["The user lives in Cardiff.", "The user no longer lives in Glasgow."],
        transcript: ["The user likes baggy jeans."],
    }
    fake = FakeExtractor(lambda text: replies.get(text, []))
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I'm living in Cardiff now.", "ok"), (transcript, "Thanks for sharing."))
    drain(bundle)
    extract(bundle)
    assert [m.content.split("] ", 1)[-1] for m in facts(bundle)] == ["The user lives in Cardiff."]
    bundle.close()


# == go-live fixes ===============================================================================
# 1. ``severian work`` also runs the fact pass, so queued fact jobs do not pile up in production.
# 2. The fact job is queued in the same transaction as the saved turn (a failed queue fails the save).
# 3. Per-profile rollback erases only the facts the switch made; raw turns, normal memories and other
#    profiles are untouched, and pending fact jobs are cancelled so nothing re-creates them.

OTHER_SESSION = SCOPE.model_copy(update={"session_id": "other"})
OTHER_PROFILE = SCOPE.model_copy(update={"profile_id": "someone-else"})
TURNS = {
    "I moved to Leeds last month.": ["The user moved to Leeds."],
    "My dog is called Biscuit.": ["The user's dog is called Biscuit."],
}


def _facts_for(text: str) -> list[str]:
    return next((f for said, f in TURNS.items() if said in text), [])


# -- 2. same transaction ------------------------------------------------------------------------


def test_switched_on_save_queues_the_fact_job_with_the_turn(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=True)
    say(bundle, ("I moved to Leeds last month.", "Nice."))
    assert len(fact_jobs(bundle)) == 1
    bundle.close()


def test_a_save_whose_fact_job_cannot_be_queued_fails_whole(tmp_path, monkeypatch):
    """No silent 'turn saved, facts never extracted' state: the save and its fact job succeed or fail together."""
    bundle = make_bundle(tmp_path, monkeypatch, on=True)

    def fail(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
        if statement.lower().lstrip().startswith("insert") and FACTS_JOB in str(parameters):
            raise RuntimeError("disk full")

    event.listen(bundle.repository.engine, "before_cursor_execute", fail)
    with pytest.raises(Exception, match="disk full"):
        say(bundle, ("I moved to Leeds last month.", "Nice."))
    event.remove(bundle.repository.engine, "before_cursor_execute", fail)
    assert bundle.repository.list_observations(SCOPE) == []
    assert fact_jobs(bundle) == []
    bundle.close()


def test_switched_off_save_queues_no_fact_job(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=False)
    say(bundle, ("I moved to Leeds last month.", "Nice."))
    assert fact_jobs(bundle) == []
    bundle.close()


# -- 1. severian work runs the fact pass --------------------------------------------------------


def _flags(folder) -> list[str]:
    return ["--database", str(folder / "s.db"), "--fts", str(folder / "s.fts"), "--vectors", str(folder / "s.vec")]


def test_severian_work_processes_queued_fact_jobs(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=True)
    say(bundle, ("I moved to Leeds last month.", "Nice."))
    bundle.close()
    fake = FakeExtractor(_facts_for)
    monkeypatch.setattr("severian.composition.FactExtractor.from_env", classmethod(lambda cls: fake))
    result = CliRunner().invoke(cli, ["work", *_flags(tmp_path)])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["facts_processed"] == 1 and summary["facts_failed"] == 0
    assert fake.calls == [[next(iter(TURNS))]]
    again = make_bundle(tmp_path, monkeypatch, on=True)
    assert [m.content.split("] ", 1)[-1] for m in facts(again)] == ["The user moved to Leeds."]
    assert fact_jobs(again)[0].status == "done"
    again.close()


def test_severian_work_leaves_fact_jobs_alone_when_the_switch_is_off(tmp_path, monkeypatch):
    """Switched off mid-flight: jobs stay queued (rollback cancels them); the model is never called."""
    bundle = make_bundle(tmp_path, monkeypatch, on=True)
    say(bundle, ("I moved to Leeds last month.", "Nice."))
    bundle.close()
    monkeypatch.delenv("SEVERIAN_USER_FACTS")
    result = CliRunner().invoke(cli, ["work", *_flags(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["facts_processed"] == 0
    again = make_bundle(tmp_path, monkeypatch, on=False)
    assert [job.status for job in fact_jobs(again)] == ["pending"]
    again.close()


# -- 3. per-profile rollback --------------------------------------------------------------------


def _populated(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=FakeExtractor(_facts_for))
    say(bundle, ("I moved to Leeds last month.", "Nice."))
    say(bundle, ("My dog is called Biscuit.", "Cute."), scope=OTHER_SESSION)
    say(bundle, ("I moved to Leeds last month.", "Nice."), scope=OTHER_PROFILE)
    drain(bundle)
    extract(bundle)
    return bundle


def _snapshot(bundle, scope: Scope) -> tuple[list[str], list[str]]:
    observations = sorted(o.raw_content for o in bundle.repository.list_observations(scope))
    others = sorted(m.content for m in bundle.repository.list_memories(scope) if m.kind != "user_fact")
    return observations, others


def test_rollback_erases_only_this_profiles_switch_facts(tmp_path, monkeypatch):
    bundle = _populated(tmp_path, monkeypatch)
    assert len(facts(bundle)) == len(facts(bundle, OTHER_SESSION)) == len(facts(bundle, OTHER_PROFILE)) == 1
    before = {scope: _snapshot(bundle, scope) for scope in (SCOPE, OTHER_SESSION, OTHER_PROFILE)}
    monkeypatch.delenv("SEVERIAN_USER_FACTS")
    report = bundle.service.rollback_user_facts(SCOPE.tenant_id, SCOPE.profile_id)
    assert report.erased == 2 and report.scopes == 2 and report.cancelled_jobs == 0
    assert facts(bundle) == facts(bundle, OTHER_SESSION) == []  # every session of the profile
    assert len(facts(bundle, OTHER_PROFILE)) == 1  # other profile untouched
    assert {scope: _snapshot(bundle, scope) for scope in before} == before  # raw turns + normal memories intact
    assert not any("The user moved to Leeds." in hit for hit in found(bundle, "where does the user live Leeds"))
    assert any("I moved to Leeds" in hit for hit in found(bundle, "Leeds"))  # the raw turn is still searchable
    bundle.close()


def test_rollback_cancels_pending_fact_jobs_so_nothing_recreates_the_facts(tmp_path, monkeypatch):
    fake = FakeExtractor(_facts_for)
    bundle = make_bundle(tmp_path, monkeypatch, on=True, extractor=fake)
    say(bundle, ("I moved to Leeds last month.", "Nice."))
    monkeypatch.delenv("SEVERIAN_USER_FACTS")
    report = bundle.service.rollback_user_facts(SCOPE.tenant_id, SCOPE.profile_id)
    assert report.cancelled_jobs == 1 and report.erased == 0
    assert extract(bundle).processed == 0 and fake.calls == [] and facts(bundle) == []
    bundle.close()


def test_rollback_is_refused_while_the_switch_is_on(tmp_path, monkeypatch):
    bundle = _populated(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="SEVERIAN_USER_FACTS"):
        bundle.service.rollback_user_facts(SCOPE.tenant_id, SCOPE.profile_id)
    assert len(facts(bundle)) == 1
    bundle.close()


def test_rollback_twice_is_a_no_op(tmp_path, monkeypatch):
    bundle = _populated(tmp_path, monkeypatch)
    monkeypatch.delenv("SEVERIAN_USER_FACTS")
    bundle.service.rollback_user_facts(SCOPE.tenant_id, SCOPE.profile_id)
    again = bundle.service.rollback_user_facts(SCOPE.tenant_id, SCOPE.profile_id)
    assert (again.erased, again.cancelled_jobs) == (0, 0)
    bundle.close()


def test_facts_rollback_command(tmp_path, monkeypatch):
    _populated(tmp_path, monkeypatch).close()
    monkeypatch.delenv("SEVERIAN_USER_FACTS")
    args = ["facts-rollback", *_flags(tmp_path), "--tenant", SCOPE.tenant_id, "--profile", SCOPE.profile_id]
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "status": "rolled_back", "tenant": SCOPE.tenant_id, "profile": SCOPE.profile_id,
        "scopes": 2, "facts_erased": 2, "fact_jobs_cancelled": 0,
    }  # fmt: skip
