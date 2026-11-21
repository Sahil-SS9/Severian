"""0.6.3: job-queue recovery, the same on both backends.

21/11: 451 ``extract_facts`` jobs ended ``failed`` for good while the local facts model was down,
and nothing noticed: ``requeue_job`` only takes ``dead_letter`` rows and the drain alarm only counts
those. The repository therefore gains four things, proved here on SQLite and live PostgreSQL:

- ``complete_job(..., "dead_letter", error=...)``: a claimed job can be dead-lettered with its reason;
- ``release_job``: a claimed job goes back to ``pending`` WITHOUT using up an attempt (the model was
  never asked);
- ``list_jobs(status=..., kind=...)``: what a recovery command plans from;
- ``requeue_jobs(ids, from_status=...)``: ``failed`` or ``dead_letter`` rows back to ``pending``
  with a fresh budget, in one transaction, one audit row each (ids and counts, never content).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import Collection, Scope
from severian.infrastructure.postgresql import PostgreSQLBackend
from severian.infrastructure.sqlite import SQLiteRepository

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")
SCOPE = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("SEVERIAN_PG_DSN not set; skipping live PostgreSQL conformance")
        repo: Any = PostgreSQLBackend(dsn=PG_DSN)
        os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
        repo.reset(acknowledge=True)
    else:
        repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    yield repo
    repo.close()


def _failed(backend: Any, observation_id: str, error: str, kind: str = "extract_facts") -> str:
    job = backend.enqueue_job(SCOPE, kind, {"observation_id": observation_id})
    backend.claim_job("w1", kind=kind)
    backend.complete_job(job.id, "failed", worker_id="w1", error=error)
    return str(job.id)


def test_a_claimed_job_can_be_dead_lettered_with_its_reason(backend: Any) -> None:
    job = backend.enqueue_job(SCOPE, "extract_facts", {"observation_id": "a"})
    backend.claim_job("w1", kind="extract_facts")
    with pytest.raises(ConflictError):
        backend.complete_job(job.id, "dead_letter", worker_id="w2", error="x")
    dead = backend.complete_job(job.id, "dead_letter", worker_id="w1", error="Connection refused")
    assert (dead.status, dead.dead_letter, dead.last_error, dead.attempts) == (
        "dead_letter", True, "Connection refused", 1
    )
    assert [j.id for j in backend.list_dead_letter_jobs()] == [job.id]
    with pytest.raises(NotFoundError):
        backend.claim_job("w3", kind="extract_facts")  # never claimed again by itself
    with pytest.raises(ValueError, match="only a failed job"):
        backend.complete_job(job.id, "dead_letter", worker_id="w1", retry_in=5)
    with pytest.raises(ValueError):
        backend.complete_job(job.id, "exploded", worker_id="w1")


def test_releasing_a_claimed_job_gives_back_its_attempt(backend: Any) -> None:
    job = backend.enqueue_job(SCOPE, "extract_facts", {"observation_id": "a"})
    claimed = backend.claim_job("w1", kind="extract_facts")
    assert claimed.attempts == 1
    with pytest.raises(ConflictError):
        backend.release_job(job.id, worker_id="w2")
    released = backend.release_job(job.id, worker_id="w1")
    assert (released.status, released.attempts, released.worker_id, released.lease_until) == ("pending", 0, None, None)
    assert released.last_error is None
    again = backend.claim_job("w2", kind="extract_facts")  # claimable at once
    assert (again.id, again.attempts) == (job.id, 1)
    with pytest.raises(NotFoundError):
        backend.release_job("no-such-job")


def test_list_jobs_filters_by_status_and_kind(backend: Any) -> None:
    failed = _failed(backend, "f", "timed out")
    pending = backend.enqueue_job(SCOPE, "extract_facts", {"observation_id": "p"})
    derive = backend.enqueue_job(SCOPE, "derive", {"observation_id": "d"})
    assert [j.id for j in backend.list_jobs(status="pending", kind="extract_facts")] == [pending.id]
    assert [j.id for j in backend.list_jobs(status="failed", kind="extract_facts")] == [failed]
    assert sorted(j.id for j in backend.list_jobs(status="pending")) == sorted([pending.id, derive.id])
    assert backend.list_jobs(status="failed", kind="derive") == []


def test_requeue_jobs_resets_failed_rows_and_audits_without_content(backend: Any) -> None:
    secret = "the user's private words"
    first = _failed(backend, "a", f"no JSON object or list in the reply (reply: {secret!r})")
    second = _failed(backend, "b", "<urlopen error [Errno 111] Connection refused>")
    untouched = _failed(backend, "c", "timed out")

    requeued = backend.requeue_jobs([first, second, "no-such-job"], from_status="failed")
    assert sorted(j.id for j in requeued) == sorted([first, second])
    for job in requeued:
        assert (job.status, job.attempts, job.worker_id, job.lease_until, job.retry_after) == (
            "pending", 0, None, None, None
        )
    assert [j.id for j in backend.list_jobs(status="failed", kind="extract_facts")] == [untouched]
    claimed = {backend.claim_job("w9", kind="extract_facts").id for _ in range(2)}
    assert claimed == {first, second}

    audit = [event for event in backend.list_audit(SCOPE) if event.action == "job.requeue"]
    assert sorted(event.target_id for event in audit) == sorted([first, second])
    for event in audit:
        assert set(event.metadata) == {"kind", "from_status", "attempts"}
        assert event.metadata["from_status"] == "failed" and event.metadata["kind"] == "extract_facts"
        assert secret not in repr(event.metadata)
    with pytest.raises(ValueError):
        backend.requeue_jobs([untouched], from_status="pending")


def test_requeue_jobs_takes_dead_letter_rows_and_refuses_other_states(backend: Any) -> None:
    job = backend.enqueue_job(SCOPE, "extract_facts", {"observation_id": "a"})
    backend.claim_job("w1", kind="extract_facts")
    backend.complete_job(job.id, "dead_letter", worker_id="w1", error="HTTP Error 503")
    assert backend.requeue_jobs([job.id], from_status="failed") == []  # wrong state: nothing
    [requeued] = backend.requeue_jobs([job.id], from_status="dead_letter")
    assert (requeued.status, requeued.attempts, requeued.dead_letter) == ("pending", 0, False)
    assert backend.list_dead_letter_jobs() == []
    with pytest.raises(ValueError):
        backend.requeue_jobs([job.id], from_status="done")
