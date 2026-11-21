"""0.6.3: user-fact jobs must not die silently while the local facts model is down.

21/11 (live store): 451 ``extract_facts`` jobs ended ``failed`` for good because the local model
was down for hours (connection refused, 503 ``no_backend``, timeouts, 503 ``Loading model``).
Every pass took a batch, called the dead endpoint, and spent an attempt; after ~6 attempts
(~2 h 15 m) the jobs failed for good, outside the drain alarm (it counts dead-letter rows).

What these tests hold the worker to, with a synthetic endpoint (never the live Carwin server):

1. before a facts pass the worker checks the endpoint once (a cheap GET); when it is down the
   jobs stay ``pending`` with their attempts unchanged and the pass reports it;
2. a "server down" fault met DURING a pass (refused, 502/503/504, timeout) leaves the rest of the
   batch pending without spending their attempts; a busy reply (409) keeps the old backoff;
3. a turn still unserved by a busy or absent model after the retry budget goes to ``dead_letter``
   (seen by ``requeue_job`` and the drain alarm), not ``failed``; an unreadable reply stays ``failed``.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from typer.testing import CliRunner

from severian.application.userfacts import FACTS_JOB, FactExtractor
from severian.application.worker import FACTS_MAX_ATTEMPTS, DerivationWorker, WorkerSummary
from severian.cli.app import cli
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Job, Scope
from severian.infrastructure.sqlite import records

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")
SCOPE = Scope(tenant_id="kensei", profile_id="sahil", collection_id="profile", agent_id="kensei", session_id="shared")
LABEL = "[2023/05/20 (Sat) 10:00]"


def no_pause(_seconds: float) -> None:
    return None


# -- a synthetic facts endpoint ---------------------------------------------------------------


def refused_url() -> str:
    """A chat URL on a local port nothing listens on: every connection is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/v1/chat/completions"


class FakeEndpoint:
    """An OpenAI-compatible server stand-in. Each route answers (status, body)."""

    def __init__(self) -> None:
        self.health: tuple[int, str] = (200, '{"status": "ok"}')
        self.models: tuple[int, str] = (200, '{"data": [{"id": "fake"}]}')
        self.chat: tuple[int, str] = (200, self.reply('{"1": ["The user lives in York."]}'))
        self.requests: list[str] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - http.server API
                return None

            def _answer(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                endpoint.requests.append(f"GET {self.path}")
                if self.path == "/health":
                    self._answer(*endpoint.health)
                elif self.path == "/v1/models":
                    self._answer(*endpoint.models)
                else:
                    self._answer(404, '{"error": "not found"}')

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                endpoint.requests.append(f"POST {self.path}")
                self._answer(*endpoint.chat)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @staticmethod
    def reply(content: str) -> str:
        return json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1/chat/completions"

    def chats(self) -> int:
        return sum(1 for r in self.requests if r.startswith("POST"))

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def endpoint() -> Iterator[FakeEndpoint]:
    server = FakeEndpoint()
    yield server
    server.close()


NO_BACKEND = (503, '{"error": "no_backend", "message": "no backend is serving carwin-moe"}')
LOADING = (503, '{"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}}')
BUSY = (409, '{"error": "request_in_progress", "message": "An identical request is already running"}')


# -- 1. the endpoint check --------------------------------------------------------------------


def test_the_check_reports_a_refused_endpoint_as_down():
    status = FactExtractor(refused_url(), sleep=no_pause).probe()
    assert status.state == "down" and "refused" in status.reason.lower()


@pytest.mark.parametrize(
    ("health", "expected"),
    [((200, '{"status": "ok"}'), "up"), (NO_BACKEND, "down"), (LOADING, "down"), ((502, "bad gateway"), "down")],
)
def test_the_check_reads_the_servers_health_route(endpoint, health, expected):
    endpoint.health = health
    status = FactExtractor(endpoint.url, sleep=no_pause).probe()
    assert status.state == expected
    assert endpoint.requests == ["GET /health"]  # derived from SEVERIAN_FACTS_URL; never a chat call


def test_without_a_health_route_the_check_falls_back_to_the_model_list(endpoint):
    endpoint.health = (404, "no such route")
    assert FactExtractor(endpoint.url, sleep=no_pause).probe().state == "up"
    endpoint.models = NO_BACKEND
    assert FactExtractor(endpoint.url, sleep=no_pause).probe().state == "down"
    assert endpoint.requests == ["GET /health", "GET /v1/models"] * 2


def test_a_check_that_cannot_tell_lets_the_pass_run(endpoint):
    """Neither route known: not proof of an outage, so the pass runs (the mid-pass rule still guards)."""
    endpoint.health = (404, "no")
    endpoint.models = (404, "no")
    assert FactExtractor(endpoint.url, sleep=no_pause).probe().state == "unknown"


def test_the_check_can_be_switched_off():
    extractor = FactExtractor.from_env({"SEVERIAN_FACTS_URL": refused_url(), "SEVERIAN_FACTS_PROBE": "0"})
    assert extractor.probe().state == "unchecked"


def test_a_refused_or_unavailable_call_marks_the_batch_as_endpoint_down(endpoint):
    got = FactExtractor(refused_url(), sleep=no_pause).extract(["I moved to York"])
    assert got.retryable == frozenset({1}) and got.endpoint_down
    endpoint.chat = NO_BACKEND
    assert FactExtractor(endpoint.url, sleep=no_pause).extract(["a"]).endpoint_down
    endpoint.chat = BUSY  # busy is not down: the old backoff applies
    busy = FactExtractor(endpoint.url, sleep=no_pause).extract(["a"])
    assert busy.retryable == frozenset({1}) and not busy.endpoint_down
    endpoint.chat = (200, FakeEndpoint.reply("not json at all"))
    unreadable = FactExtractor(endpoint.url, sleep=no_pause).extract(["a"])
    assert unreadable.failed and not unreadable.retryable and not unreadable.endpoint_down


# -- 2. the worker ----------------------------------------------------------------------------


def make_bundle(tmp_path: Path, monkeypatch, url: str, *, backend: str = "sqlite") -> ServiceBundle:
    monkeypatch.setenv("SEVERIAN_USER_FACTS", "1")
    monkeypatch.setenv("SEVERIAN_FACTS_URL", url)
    if backend == "postgresql":
        built = build_bundle(backend="postgresql", dsn=PG_DSN)
    else:
        tmp_path.mkdir(parents=True, exist_ok=True)
        built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    built.derivation._fact_extractor = FactExtractor(url, sleep=no_pause, timeout=5)
    return built


def say(bundle: ServiceBundle, *users: str, scope: Scope = SCOPE) -> None:
    for number, user in enumerate(users, 1):
        bundle.service.ingest_observation(
            scope, content=f"{LABEL} user: {user}\nassistant: ok",
            source={"session_id": "s1", "turn": number, "timestamp": "2023-05-20T10:00:00+00:00"},
        )


def worker(bundle: ServiceBundle) -> DerivationWorker:
    return DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)


def drain(bundle: ServiceBundle) -> None:
    assert worker(bundle).drain(gateway=bundle.gateway).failed == 0


def fact_jobs(bundle: ServiceBundle) -> list[Job]:
    jobs = [*bundle.repository.list_jobs(status="pending", kind=FACTS_JOB)]
    for status in ("running", "done", "failed", "dead_letter"):
        jobs += bundle.repository.list_jobs(status=status, kind=FACTS_JOB)
    return jobs


def _due_now(bundle: ServiceBundle) -> None:
    with bundle.repository.engine.begin() as connection:  # type: ignore[attr-defined]
        connection.execute(update(records).where(records.c.record_type == "job").values(retry_after=None))


def test_a_down_endpoint_leaves_every_fact_job_pending_without_spending_attempts(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, refused_url())
    say(bundle, "I moved to York", "I adopted a dog named Max", "I prefer aisle seats")
    drain(bundle)
    for _ in range(FACTS_MAX_ATTEMPTS + 3):  # far past the old budget: nothing may fail or dead-letter
        summary = worker(bundle).extract_facts()
        assert (summary.processed, summary.failed, summary.gave_up, summary.dead_lettered) == (0, 0, 0, 0)
        assert summary.deferred == 3 and summary.endpoint == "down"
        _due_now(bundle)
    jobs = fact_jobs(bundle)
    assert [(job.status, job.attempts, job.last_error) for job in jobs] == [("pending", 0, None)] * 3
    assert bundle.repository.list_dead_letter_jobs() == []
    bundle.close()


def test_the_jobs_run_normally_once_the_endpoint_is_back(tmp_path, monkeypatch, endpoint):
    bundle = make_bundle(tmp_path, monkeypatch, refused_url())
    say(bundle, "I moved to York")
    drain(bundle)
    assert worker(bundle).extract_facts().endpoint == "down"
    bundle.derivation._fact_extractor = FactExtractor(endpoint.url, sleep=no_pause, timeout=5)
    summary = worker(bundle).extract_facts()
    assert (summary.processed, summary.deferred, summary.endpoint) == (1, 0, "up")
    assert [(job.status, job.attempts) for job in fact_jobs(bundle)] == [("done", 1)]
    bundle.close()


def test_an_outage_met_during_a_pass_defers_the_rest_of_the_batch(tmp_path, monkeypatch, endpoint):
    """Health says OK (a gateway in front of a dead backend), the chat call says no_backend."""
    endpoint.chat = NO_BACKEND
    bundle = make_bundle(tmp_path, monkeypatch, endpoint.url)
    for name in ("alice", "bob", "carol"):  # three users: three model calls in one batch
        say(bundle, f"I am {name} and I moved to York", scope=SCOPE.model_copy(update={"profile_id": name}))
    drain(bundle)
    summary = worker(bundle).extract_facts(batch_size=2)
    assert summary.endpoint == "down" and summary.deferred == 3
    assert (summary.processed, summary.failed, summary.gave_up, summary.dead_lettered) == (0, 0, 0, 0)
    assert endpoint.chats() == 2  # the first user's call and its one retry; nobody else was sent
    attempts = sorted((job.status, job.attempts) for job in fact_jobs(bundle))
    # the user whose call hit the outage keeps the old backoff (one attempt); the other two spent none
    assert attempts == [("pending", 0), ("pending", 0), ("pending", 1)]
    bundle.close()


def test_a_busy_model_keeps_the_old_backoff_and_the_pass_goes_on(tmp_path, monkeypatch, endpoint):
    endpoint.chat = BUSY
    bundle = make_bundle(tmp_path, monkeypatch, endpoint.url)
    for name in ("alice", "bob"):
        say(bundle, f"I am {name}", scope=SCOPE.model_copy(update={"profile_id": name}))
    drain(bundle)
    summary = worker(bundle).extract_facts()
    assert (summary.deferred, summary.endpoint) == (2, "up") and endpoint.chats() == 4
    assert sorted((job.status, job.attempts) for job in fact_jobs(bundle)) == [("pending", 1), ("pending", 1)]
    bundle.close()


def test_a_model_still_unavailable_after_the_budget_dead_letters_the_turn(tmp_path, monkeypatch, endpoint):
    endpoint.chat = BUSY
    bundle = make_bundle(tmp_path, monkeypatch, endpoint.url)
    say(bundle, "I moved to York")
    drain(bundle)
    for _ in range(FACTS_MAX_ATTEMPTS - 1):
        assert worker(bundle).extract_facts().deferred == 1
        _due_now(bundle)
    summary = worker(bundle).extract_facts()
    assert (summary.dead_lettered, summary.gave_up, summary.deferred) == (1, 0, 0)
    [job] = fact_jobs(bundle)
    assert job.status == "dead_letter" and job.dead_letter and job.attempts == FACTS_MAX_ATTEMPTS
    assert "409" in (job.last_error or "")
    assert [j.id for j in bundle.repository.list_dead_letter_jobs()] == [job.id]  # the drain alarm counts these
    bundle.close()


def test_an_unreadable_reply_still_fails_with_its_reason(tmp_path, monkeypatch, endpoint):
    endpoint.chat = (200, FakeEndpoint.reply("I cannot do that"))
    bundle = make_bundle(tmp_path, monkeypatch, endpoint.url)
    say(bundle, "I moved to York")
    drain(bundle)
    summary = worker(bundle).extract_facts()
    assert (summary.gave_up, summary.dead_lettered, summary.endpoint) == (1, 0, "up")
    [job] = fact_jobs(bundle)
    assert job.status == "failed" and job.attempts == 1 and "no JSON" in (job.last_error or "")
    bundle.close()


def test_an_idle_queue_never_calls_the_endpoint(tmp_path, monkeypatch, endpoint):
    bundle = make_bundle(tmp_path, monkeypatch, endpoint.url)
    summary = worker(bundle).extract_facts()
    assert summary == WorkerSummary(endpoint="unchecked") and endpoint.requests == []
    bundle.close()


@pytest.mark.skipif(not PG_DSN, reason="SEVERIAN_PG_DSN not set; skipping live PostgreSQL lane")
def test_postgresql_defers_without_attempts_and_dead_letters_after_the_budget(tmp_path, monkeypatch, endpoint):
    from severian.infrastructure.postgresql import PostgreSQLBackend

    os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
    reset = PostgreSQLBackend(dsn=PG_DSN)
    reset.reset(acknowledge=True)
    reset.close()
    bundle = make_bundle(tmp_path, monkeypatch, refused_url(), backend="postgresql")
    say(bundle, "I moved to York")
    drain(bundle)
    for _ in range(FACTS_MAX_ATTEMPTS + 1):
        assert worker(bundle).extract_facts().endpoint == "down"
    assert [(j.status, j.attempts) for j in fact_jobs(bundle)] == [("pending", 0)]

    endpoint.chat = BUSY
    bundle.derivation._fact_extractor = FactExtractor(endpoint.url, sleep=no_pause, timeout=5)
    for _ in range(FACTS_MAX_ATTEMPTS):
        summary = worker(bundle).extract_facts()
        bundle.repository.reap_expired_jobs()
        with bundle.repository.engine.begin() as connection:  # type: ignore[attr-defined]
            connection.exec_driver_sql("UPDATE records SET retry_after = NULL WHERE record_type = 'job'")
    assert summary.dead_lettered == 1
    [job] = bundle.repository.list_dead_letter_jobs()
    assert job.attempts == FACTS_MAX_ATTEMPTS
    bundle.close()


# -- the work command's summary -----------------------------------------------------------------


def _flags(folder: Path) -> list[str]:
    return ["--database", str(folder / "s.db"), "--fts", str(folder / "s.fts"), "--vectors", str(folder / "s.vec")]


def test_severian_work_reports_the_endpoint_and_dead_letters(tmp_path, monkeypatch):
    bundle = make_bundle(tmp_path, monkeypatch, refused_url())
    say(bundle, "I moved to York")
    bundle.close()
    result = CliRunner().invoke(cli, ["work", *_flags(tmp_path)])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["facts_endpoint"] == "down" and summary["facts_deferred"] == 1
    assert summary["facts_dead_lettered"] == 0 and summary["facts_gave_up"] == 0
    check = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    with check.repository.engine.connect() as connection:  # type: ignore[attr-defined]
        rows = connection.execute(select(records.c.status, records.c.payload).where(records.c.record_type == "job"))
        facts = [(status, payload["attempts"]) for status, payload in rows if payload["kind"] == FACTS_JOB]
    assert facts == [("pending", 0)]
    check.close()


def test_severian_work_output_is_unchanged_with_the_switch_off(tmp_path, monkeypatch):
    monkeypatch.delenv("SEVERIAN_USER_FACTS", raising=False)
    result = CliRunner().invoke(cli, ["work", *_flags(tmp_path)])
    assert result.exit_code == 0, result.output
    keys = set(json.loads(result.output))
    assert "facts_endpoint" not in keys and "facts_dead_lettered" not in keys
