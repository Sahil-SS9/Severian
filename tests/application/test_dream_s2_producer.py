"""S2 producer: the switch, the queueing tick, and dream jobs as a background kind.

Nothing in the store queued a dream job at 0.4.1 — ``DerivationWorker._derive`` could run one if a
row happened to exist, and no path ever wrote one. These tests pin the two entry points the item
adds, both behind ``SEVERIAN_DREAMING`` (off by default), and the drain behaviour on both sides of
the switch.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from severian.application.dreamqueue import (
    DREAM_JOB,
    DreamProducer,
    dream_background_kinds,
    dreaming_enabled,
)
from severian.application.dreamwriter import DreamWriter
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Memory, Observation, Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="s2", collection_id="profile", agent_id="s2", session_id="shared"
)


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def remember(bundle: ServiceBundle, content: str, *, scope: Scope = SCOPE) -> Memory:
    observation = bundle.repository.add_observation(
        Observation(scope=scope, content=content, raw_content=content, source={"uri": f"note://{uuid.uuid4().hex}"})
    )
    return bundle.repository.add_memory(
        Memory(
            scope=scope,
            kind="fact",
            content=content,
            source_observation_ids=[observation.id],
            model_id="test",
            prompt_version="p1",
            confidence=0.9,
        )
    )


def duplicate_pair(bundle: ServiceBundle, content: str = "the user keeps a steel kettle") -> None:
    remember(bundle, content)
    remember(bundle, content)


def test_the_dreaming_switch_is_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_DREAMING", raising=False)
    assert dreaming_enabled() is False
    assert dream_background_kinds() == ()
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    assert dreaming_enabled() is True
    assert dream_background_kinds() == (DREAM_JOB,)


def test_the_writer_refuses_a_remote_endpoint_unless_allowed() -> None:
    """The default path cannot spend money: a remote model needs an explicit opt-in."""
    with pytest.raises(ValueError, match="SEVERIAN_DREAM_ALLOW_REMOTE"):
        DreamWriter("https://api.example.com/v1/chat/completions")
    assert DreamWriter("https://api.example.com/v1/chat/completions", allow_remote=True).url.startswith("https://")


def test_the_tick_queues_one_job_per_scope_and_mode(bundle: ServiceBundle) -> None:
    remember(bundle, "the user keeps a steel kettle")
    producer = DreamProducer(bundle.repository, modes=("consolidate", "curate"))
    result = producer.queue_profile(SCOPE.tenant_id, SCOPE.profile_id)
    assert result.scopes == 1
    assert len(result.jobs) == 2
    claimed = bundle.repository.claim_job("probe", kind=DREAM_JOB)
    assert claimed.kind == DREAM_JOB
    assert claimed.payload["mode"] in {"consolidate", "curate"}


def test_the_tick_is_idempotent(bundle: ServiceBundle) -> None:
    """A second tick queues nothing: enqueue_job's identity key is (kind, payload)."""
    remember(bundle, "the user keeps a steel kettle")
    producer = DreamProducer(bundle.repository, modes=("consolidate", "curate"))
    first = producer.queue_profile(SCOPE.tenant_id, SCOPE.profile_id)
    second = producer.queue_profile(SCOPE.tenant_id, SCOPE.profile_id)
    assert first.jobs == second.jobs


def test_the_tick_does_not_queue_retention(bundle: ServiceBundle) -> None:
    """Pruning stays an explicit operator action (decision 5)."""
    remember(bundle, "the user keeps a steel kettle")
    queued = DreamProducer(bundle.repository).queue_profile(SCOPE.tenant_id, SCOPE.profile_id)
    assert "retention" not in queued.modes and "retention-archive" not in queued.modes


def test_an_unknown_mode_is_refused(bundle: ServiceBundle) -> None:
    with pytest.raises(ValueError, match="unknown dream mode"):
        DreamProducer(bundle.repository, modes=("dream-a-little",))


def test_the_derive_drain_leaves_dream_jobs_alone_while_dreaming_is_on(
    bundle: ServiceBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    remember(bundle, "the user keeps a steel kettle")
    bundle.repository.enqueue_job(SCOPE, DREAM_JOB, {"mode": "contradictions"})
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).processed == 0
    # Still queued: the derive pass did not swallow it.
    assert bundle.repository.claim_job("probe", kind=DREAM_JOB).status == "running"


def test_the_derive_drain_still_takes_dream_jobs_with_the_switch_off(bundle: ServiceBundle) -> None:
    """0.4.1 behaviour, unchanged: with the switch off nothing about the drain moves."""
    remember(bundle, "the user keeps a steel kettle")
    bundle.repository.enqueue_job(SCOPE, DREAM_JOB, {"mode": "contradictions"})
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).processed == 1


def test_the_dream_pass_refuses_while_the_switch_is_off(bundle: ServiceBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    """The switch is a boundary in the library too, not only in the CLI."""
    monkeypatch.delenv("SEVERIAN_DREAMING", raising=False)
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    with pytest.raises(RuntimeError, match="SEVERIAN_DREAMING"):
        worker.dream_pass()


def test_the_dream_pass_runs_a_queued_job(bundle: ServiceBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    duplicate_pair(bundle)
    bundle.repository.enqueue_job(SCOPE, DREAM_JOB, {"mode": "consolidate"})
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    summary = worker.dream_pass()
    assert (summary.processed, summary.failed) == (1, 0)
    assert {memory.kind for memory in bundle.repository.list_memories(SCOPE)} >= {"consolidated"}


def test_the_dream_pass_projects_what_it_wrote(bundle: ServiceBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    """A marker is hidden from search by ``markers``, not by leaving the projection stale."""
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    duplicate_pair(bundle)
    bundle.repository.enqueue_job(SCOPE, DREAM_JOB, {"mode": "curate"})
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.dream_pass().processed == 1
    assert bundle.repository.pending_outbox() == []


def test_the_dream_tick_queues_and_runs_in_one_pass(bundle: ServiceBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    duplicate_pair(bundle)
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    queued, summary = worker.dream_tick(tenant_id=SCOPE.tenant_id, profile_id=SCOPE.profile_id)
    assert queued.jobs
    assert summary.processed == len(queued.jobs)
    assert summary.failed == 0
    # A second tick queues nothing and runs nothing: the queue converged.
    second_queued, second_summary = worker.dream_tick(tenant_id=SCOPE.tenant_id, profile_id=SCOPE.profile_id)
    assert second_queued.jobs == queued.jobs
    assert second_summary.processed == 0
