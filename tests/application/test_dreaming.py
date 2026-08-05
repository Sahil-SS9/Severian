"""Tests for dreaming (consolidation) and self-curation (2026-08-05).

Proves:
- near-duplicate memories are merged non-destructively (older superseded,
  survivor keeps provenance, consolidated memory emitted, audit recorded);
- contradiction candidates are flagged without mutation;
- curation scores salience deterministically and NEVER deletes;
- the worker's ``dream`` job kind (modes consolidate/curate) closes the
  queue loop;
- both passes are scope-bound (other scopes untouched).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from severian.application.dreaming import (
    ConsolidationService,
    CurationService,
    jaccard,
)
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.worker import DerivationWorker
from severian.domain.errors import NotFoundError
from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _bundle(tmp: str):
    root = Path(tmp)
    repo = SQLiteRepository(path=root / "sev.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(root / "fts.db")
    vec = LocalVectorIndex(root / "vec.db", embedding=HashEmbedding())
    proj = ProjectionCoordinator(repository=repo, lexical=fts, vector=vec)
    ingest = IngestionService(repository=repo)
    return repo, fts, vec, proj, ingest


def _scope() -> Scope:
    return Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1"
    )


def _add_memory(
    repo: SQLiteRepository, scope: Scope, content: str, *, kind: str = "fact",
    created_delta: timedelta | None = None, confidence: float = 0.9,
) -> Memory:
    # A memory must reference a REAL observation (source validation).
    # A unique source URI guarantees a distinct observation per call.
    import uuid

    obs = repo.add_observation(
        Observation(
            scope=scope,
            content=content,
            raw_content=content,
            source={"uri": f"note://{uuid.uuid4().hex[:12]}"},
        )
    )
    memory = Memory(
        scope=scope,
        kind=kind,
        content=content,
        source_observation_ids=[obs.id],
        model_id="test",
        prompt_version="p1",
        confidence=confidence,
    )
    if created_delta is not None:
        memory = memory.model_copy(update={"created_at": datetime.now(UTC) - created_delta})
    return repo.add_memory(memory)


def test_jaccard_basics() -> None:
    assert jaccard("a b c d", "a b c d") == 1.0
    # intersection {a,b,c}=3, union {a,b,c,d,e}=5 -> 3/5 = 0.6
    assert jaccard("a b c d", "a b c e") == 0.6
    assert jaccard("a b", "c d") == 0.0


def test_consolidation_merges_near_duplicates_non_destructively() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        older = _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=10))
        newer = _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=1))
        service = ConsolidationService(repo)
        report = service.consolidate(scope)

        assert len(report.duplicate_groups) == 1
        group = report.duplicate_groups[0]
        assert group.survivor_id == newer.id
        assert group.superseded_ids == (older.id,)
        # Non-destructive: older remains in history, marked superseded.
        superseded = repo.get_memory(scope, older.id)
        assert superseded.superseded_by_id == newer.id
        # Survivor still active.
        active = repo.list_memories(scope)
        assert newer.id in {m.id for m in active}
        assert older.id not in {m.id for m in active}
        # Consolidated memory emitted + audit recorded.
        assert len(report.consolidated_memory_ids) == 1
        assert report.audit_events >= 1


def test_consolidation_is_idempotent_on_rerun() -> None:
    """OPS-05: running consolidate twice on the same scope must not
    crash (no ConflictError on re-superseding) and must not emit a
    duplicate consolidated memory."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=10))
        _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=1))
        service = ConsolidationService(repo)

        first = service.consolidate(scope)
        assert len(first.duplicate_groups) == 1
        assert len(first.consolidated_memory_ids) == 1

        # Second pass: same scope, no new work, no crash, no duplicate.
        second = service.consolidate(scope)
        assert second.duplicate_groups == []
        consolidated_kinds = [
            m for m in repo.list_memories_history(scope) if m.kind == "consolidated"
        ]
        # Exactly ONE consolidated memory exists (first pass only).
        assert len(consolidated_kinds) == 1


def test_consolidation_flags_contradictions_without_mutation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "the office address is 10 downing street")
        _add_memory(repo, scope, "the office address is 20 downing street")
        service = ConsolidationService(repo)
        report = service.consolidate(scope)
        # High overlap (only the street number differs), same surface ->
        # contradiction candidate, no merge.
        assert report.contradictions, "expected contradiction candidates"
        assert not report.duplicate_groups
        active = repo.list_memories(scope)
        assert len(active) == 2  # nothing mutated


def test_consolidation_is_scope_bound() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope_a = _scope()
        scope_b = Scope(
            tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a2", session_id="s1"
        )
        _add_memory(repo, scope_a, "duplicate content here")
        _add_memory(repo, scope_a, "duplicate content here")
        _add_memory(repo, scope_b, "duplicate content here")
        _add_memory(repo, scope_b, "duplicate content here")
        service = ConsolidationService(repo)
        report = service.consolidate(scope_a)
        assert len(report.duplicate_groups) == 1
        # Scope B untouched: both memories still active there.
        active_b = repo.list_memories(scope_b)
        assert len(active_b) == 2


def test_curation_scores_and_proposes_never_deletes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        fresh_high = _add_memory(
            repo, scope, "critical recent fact with high confidence", created_delta=timedelta(hours=1)
        )
        old_low = _add_memory(
            repo, scope, "stale low value", created_delta=timedelta(days=400), confidence=0.05
        )
        service = CurationService(repo, now=datetime.now(UTC))
        report = service.curate(scope)

        assert report.scored, "expected scored memories"
        by_id = {entry["memory_id"]: entry for entry in report.scored}
        assert by_id[fresh_high.id]["salience"] > by_id[old_low.id]["salience"]
        # The old low-confidence memory is a low-value CANDIDATE, not deleted.
        assert old_low.id in report.low_value_ids
        assert repo.get_memory(scope, old_low.id).superseded_by_id is None
        # Review marker written.
        assert len(report.review_marker_ids) == 1


def test_worker_dream_job_kind_consolidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj, ingest = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=10))
        _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=1))
        repo.enqueue_job(scope, "dream", {"mode": "consolidate"})
        worker = DerivationWorker(repository=repo, derivation=None, projection=proj)
        summary = worker.drain(gateway=FakeGateway())
        assert summary.processed == 1
        active = repo.list_memories(scope)
        assert len(active) == 1 + 1  # survivor + consolidated memory
        kinds = {m.kind for m in active}
        assert "consolidated" in kinds


def test_worker_dream_job_kind_curate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj, ingest = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "some memory content here", created_delta=timedelta(days=400))
        repo.enqueue_job(scope, "dream", {"mode": "curate"})
        worker = DerivationWorker(repository=repo, derivation=None, projection=proj)
        summary = worker.drain(gateway=FakeGateway())
        assert summary.processed == 1
        kinds = {m.kind for m in repo.list_memories(scope)}
        assert "curation_review" in kinds


def test_consolidation_preflight_aborts_before_any_write() -> None:
    """OPS-06: consolidation failure-atomicity — if pre-flight validation
    fails (a survivor's source observation is unresolvable), the pass
    aborts BEFORE any write: no supersedes, no consolidated memory, no
    audit events — the scope is left exactly as it was."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        older = _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=10))
        _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=1))
        service = ConsolidationService(repo)

        # Force pre-flight failure: the survivor's source observation
        # becomes unresolvable mid-consolidation (simulated corruption).
        real_get = repo.get_observation

        def _flaky_get_observation(sc, obs_id):
            if obs_id == older.source_observation_ids[0]:
                raise NotFoundError("simulated corrupt source")
            return real_get(sc, obs_id)

        repo.get_observation = _flaky_get_observation  # type: ignore[method-assign]
        try:
            with pytest.raises(ValueError, match="aborted before any write"):
                service.consolidate(scope)
        finally:
            repo.get_observation = real_get  # type: ignore[method-assign]

        # Nothing mutated: both memories active, no consolidated memory,
        # no audit events from the aborted pass.
        active = repo.list_memories(scope)
        assert len(active) == 2
        assert repo.get_memory(scope, older.id).superseded_by_id is None
