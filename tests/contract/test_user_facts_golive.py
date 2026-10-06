"""Go-live storage contract for user facts (both backends; Postgres needs SEVERIAN_PG_DSN).

1. The fact job commits in the SAME transaction as the saved turn (and a correction's revision).
2. Rollback support: list a profile's scopes; cancel its pending fact jobs; refuse while one is running.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import event

from severian.application.userfacts import FACTS_JOB
from severian.domain.models import Collection, Observation, Scope
from severian.infrastructure.postgresql import PostgreSQLBackend
from severian.infrastructure.sqlite import ConflictError, SQLiteRepository

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")


def _scope(profile: str = "p1", session: str = "s1", agent: str = "a1") -> Scope:
    return Scope(tenant_id="t1", profile_id=profile, collection_id="c1", agent_id=agent, session_id=session)


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path):
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("SEVERIAN_PG_DSN not set; skipping live PostgreSQL")
        repo = PostgreSQLBackend(dsn=PG_DSN)
        os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
        repo.reset(acknowledge=True)
    else:
        repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    yield repo
    repo.close()


def _kinds(repo, scope: Scope | None = None) -> list[str]:
    """Claim every pending job (optionally only ``scope``'s) and return their kinds, sorted."""
    kinds, others = [], []
    while True:
        try:
            job = repo.claim_job("probe")
        except Exception:
            break
        (kinds if scope is None or job.scope == scope else others).append(job.kind)
    return sorted(kinds)


def _fail_fact_job_insert(conn, cursor, statement, parameters, context, executemany):  # noqa: ARG001
    """Fail the INSERT of a fact job. Postgres wraps the JSON payload (``.obj``), SQLite passes text."""
    payload = parameters.get("payload") if isinstance(parameters, dict) else parameters
    payload = getattr(payload, "obj", payload)
    if statement.lower().lstrip().startswith("insert") and FACTS_JOB in str(payload):
        raise RuntimeError("disk full")


def test_saved_turn_queues_derive_and_fact_jobs(backend):
    _, job = backend.add_observation_and_enqueue(
        Observation(scope=_scope(), content="I moved to Leeds."), also_queue=(FACTS_JOB,)
    )
    assert job.kind == "derive"
    assert _kinds(backend) == ["derive", FACTS_JOB]


def test_no_saved_turn_without_its_fact_job(backend):
    """Same transaction: when the fact job's write fails, the turn's write is undone too."""
    event.listen(backend.engine, "before_cursor_execute", _fail_fact_job_insert)
    try:
        with pytest.raises(Exception, match="disk full"):
            backend.add_observation_and_enqueue(
                Observation(scope=_scope(), content="I moved."), also_queue=(FACTS_JOB,)
            )
    finally:
        event.remove(backend.engine, "before_cursor_execute", _fail_fact_job_insert)
    assert backend.list_observations(_scope()) == []
    assert _kinds(backend) == []


def test_no_correction_without_its_fact_job(backend):
    original, _ = backend.add_observation_and_enqueue(Observation(scope=_scope(), content="I live in York."))
    _kinds(backend)
    event.listen(backend.engine, "before_cursor_execute", _fail_fact_job_insert)
    try:
        with pytest.raises(Exception, match="disk full"):
            backend.revise_observation(_scope(), original.id, "I live in Leeds.", derive=True, also_queue=(FACTS_JOB,))
    finally:
        event.remove(backend.engine, "before_cursor_execute", _fail_fact_job_insert)
    assert [o.id for o in backend.list_observations(_scope())] == [original.id]  # old head still active
    backend.revise_observation(_scope(), original.id, "I live in Leeds.", derive=True, also_queue=(FACTS_JOB,))
    assert _kinds(backend) == ["derive", FACTS_JOB]


def test_reingest_queues_a_missing_fact_job_once(backend):
    first, _ = backend.add_observation_and_enqueue(Observation(scope=_scope(), content="Hi."))
    again, _ = backend.add_observation_and_enqueue(Observation(scope=_scope(), content="Hi."), also_queue=(FACTS_JOB,))
    backend.add_observation_and_enqueue(Observation(scope=_scope(), content="Hi."), also_queue=(FACTS_JOB,))
    assert again.id == first.id
    assert _kinds(backend) == ["derive", FACTS_JOB]


def test_profile_scopes_cover_every_session_and_agent_of_that_profile_only(backend):
    for scope in (_scope(session="s1"), _scope(session="s2"), _scope(agent="a2"), _scope(profile="p2")):
        backend.add_observation_and_enqueue(Observation(scope=scope, content=f"hello {scope.session_id}"))
    got = {(s.agent_id, s.session_id) for s in backend.list_profile_scopes("t1", "p1")}
    assert got == {("a1", "s1"), ("a1", "s2"), ("a2", "s1")}


def test_cancel_pending_fact_jobs_leaves_other_kinds_and_profiles(backend):
    for scope in (_scope(), _scope(profile="p2")):
        backend.add_observation_and_enqueue(Observation(scope=scope, content="x"), also_queue=(FACTS_JOB,))
    assert backend.cancel_pending_jobs(_scope(), FACTS_JOB) == 1
    assert backend.cancel_pending_jobs(_scope(), FACTS_JOB) == 0
    remaining = []
    while True:
        try:
            job = backend.claim_job("probe")
        except Exception:
            break
        remaining.append((job.scope.profile_id, job.kind))
    assert sorted(remaining) == [("p1", "derive"), ("p2", "derive"), ("p2", FACTS_JOB)]


def test_cancel_refuses_while_a_fact_job_is_running(backend):
    backend.add_observation_and_enqueue(Observation(scope=_scope(), content="x"), also_queue=(FACTS_JOB,))
    backend.claim_job("w", kind=FACTS_JOB)
    with pytest.raises(ConflictError, match="running"):
        backend.cancel_pending_jobs(_scope(), FACTS_JOB)
