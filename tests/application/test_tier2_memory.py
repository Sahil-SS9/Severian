"""Tests for Tier 2 AI-memory procedures (2026-08-05).

Covers:
- usage-count tracking (record_memory_usage, active-only, scope-bound)
- salience-weighted retrieval ranking (opt-in, deterministic)
- retention policy evaluation + opt-in non-destructive archive
- standalone contradiction scan
- worker dream modes: retention, retention-archive, contradictions
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from severian.application.dreaming import (
    ConsolidationService,
    ContradictionScanService,
    CurationService,
    RetentionPolicyService,
)
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import RetrievalService
from severian.application.worker import DerivationWorker
from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


def _bundle(tmp: str):
    root = Path(tmp)
    repo = SQLiteRepository(path=root / "sev.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(root / "fts.db")
    vec = LocalVectorIndex(root / "vec.db", embedding=HashEmbedding())
    proj = ProjectionCoordinator(repository=repo, lexical=fts, vector=vec)
    return repo, fts, vec, proj


def _scope() -> Scope:
    return Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1"
    )


def _add_memory(
    repo: SQLiteRepository, scope: Scope, content: str, *, kind: str = "fact",
    created_delta: timedelta | None = None, confidence: float = 0.9,
) -> Memory:
    import uuid

    obs = repo.add_observation(
        Observation(
            scope=scope, content=content, raw_content=content,
            source={"uri": f"note://{uuid.uuid4().hex[:12]}"},
        )
    )
    memory = Memory(
        scope=scope, kind=kind, content=content,
        source_observation_ids=[obs.id], model_id="test", prompt_version="p1",
        confidence=confidence,
    )
    if created_delta is not None:
        memory = memory.model_copy(update={"created_at": datetime.now(UTC) - created_delta})
    return repo.add_memory(memory)


# --- usage tracking ---------------------------------------------------------


def test_record_memory_usage_increments() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        memory = _add_memory(repo, scope, "a useful fact")
        assert memory.usage_count == 0
        updated = repo.record_memory_usage(scope, memory.id)
        assert updated.usage_count == 1
        updated2 = repo.record_memory_usage(scope, memory.id)
        assert updated2.usage_count == 2


def test_record_memory_usage_skips_superseded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        old = _add_memory(repo, scope, "duplicate content xyz", created_delta=timedelta(days=5))
        new = _add_memory(repo, scope, "duplicate content xyz", created_delta=timedelta(days=1))
        ConsolidationService(repo).consolidate(scope)
        # old is now superseded -> usage not recorded.
        result = repo.record_memory_usage(scope, old.id)
        assert result.usage_count == 0
        # new is active -> usage recorded.
        result2 = repo.record_memory_usage(scope, new.id)
        assert result2.usage_count == 1


# --- salience-weighted ranking ----------------------------------------------


def test_salience_weight_changes_ranking_deterministically() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope = _scope()
        # Two memories with IDENTICAL relevance to a query, different recency.
        old_mem = _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=30))
        new_mem = _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=1))
        proj.rebuild(scope)  # index the memories into FTS + vector
        retrieval = RetrievalService(repo, fts, vec)
        now = datetime.now(UTC)

        # Without salience: equal fused scores -> content tiebreak (identical
        # content) -> older id first (stable, deterministic).
        plain = retrieval.search(scope, "search index sqlite fts5", limit=5, now=now)
        plain_ids = [r.memory.id for r in plain]

        # With salience: the RECENT memory is boosted above the old one.
        weighted = retrieval.search(
            scope, "search index sqlite fts5", limit=5,
            salience_weight=1.0, salience_halflife_h=24.0, now=now,
        )
        weighted_ids = [r.memory.id for r in weighted]

        assert new_mem.id in plain_ids and old_mem.id in plain_ids
        # Deterministic: same inputs -> same order every call.
        weighted2 = retrieval.search(
            scope, "search index sqlite fts5", limit=5,
            salience_weight=1.0, salience_halflife_h=24.0, now=now,
        )
        assert [r.memory.id for r in weighted2] == weighted_ids
        # The boost moved the recent memory to the front.
        assert weighted_ids[0] == new_mem.id


def test_salience_zero_is_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "alpha beta gamma delta epsilon")
        _add_memory(repo, scope, "alpha beta gamma delta epsilon")
        proj.rebuild(scope)
        retrieval = RetrievalService(repo, fts, vec)
        plain = retrieval.search(scope, "alpha beta gamma", limit=5)
        weighted = retrieval.search(scope, "alpha beta gamma", limit=5, salience_weight=0.0)
        assert [r.memory.id for r in plain] == [r.memory.id for r in weighted]


# --- retention policy -------------------------------------------------------


def test_retention_evaluate_reports_candidates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        old = _add_memory(repo, scope, "old stale fact", created_delta=timedelta(days=400))
        fresh = _add_memory(repo, scope, "recent important fact", created_delta=timedelta(days=1))
        service = RetentionPolicyService(repo, now=datetime.now(UTC))
        result = service.evaluate(scope, {"max_age_days": 365})
        assert old.id in result.archived_ids
        assert fresh.id in result.kept_ids
        # Nothing mutated.
        assert repo.get_memory(scope, old.id).superseded_by_id is None


def test_retention_min_usage_keeps_highly_used() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        old_used = _add_memory(repo, scope, "old but frequently used fact", created_delta=timedelta(days=400))
        old_unused = _add_memory(repo, scope, "old never used fact", created_delta=timedelta(days=400))
        repo.record_memory_usage(scope, old_used.id)
        repo.record_memory_usage(scope, old_used.id)
        repo.record_memory_usage(scope, old_used.id)
        service = RetentionPolicyService(repo, now=datetime.now(UTC))
        result = service.evaluate(scope, {"max_age_days": 365, "min_usage": 3})
        assert old_unused.id in result.archived_ids
        assert old_used.id in result.kept_ids  # usage >= 3 saves it


def test_retention_apply_archive_is_opt_in_non_destructive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        old = _add_memory(repo, scope, "expired fact one", created_delta=timedelta(days=500))
        fresh = _add_memory(repo, scope, "fresh fact", created_delta=timedelta(days=1))
        service = RetentionPolicyService(repo, now=datetime.now(UTC))
        result = service.apply_archive(scope, {"max_age_days": 365})
        assert old.id in result.expired_ids
        # Old head superseded by the retention marker.
        assert repo.get_memory(scope, old.id).superseded_by_id is not None
        # Fresh untouched.
        assert repo.get_memory(scope, fresh.id).superseded_by_id is None
        # Marker memory exists in history; active list excludes old.
        active = repo.list_memories(scope)
        assert old.id not in {m.id for m in active}
        kinds = {m.kind for m in active}
        assert "retention_archive" in kinds


def test_retention_apply_archive_is_idempotent_on_rerun() -> None:
    """OPS-05: a second apply_archive on the same scope must not crash
    (no ConflictError on already-archived memories) and must not create a
    duplicate retention marker."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "expired fact one", created_delta=timedelta(days=500))
        service = RetentionPolicyService(repo, now=datetime.now(UTC))

        first = service.apply_archive(scope, {"max_age_days": 365})
        assert first.expired_ids

        # Second pass: same scope, no crash, no duplicate marker.
        second = service.apply_archive(scope, {"max_age_days": 365})
        assert second.expired_ids == []
        markers = [
            m for m in repo.list_memories_history(scope) if m.kind == "retention_archive"
        ]
        assert len(markers) == 1


# --- contradiction scan -----------------------------------------------------


def test_contradiction_scan_standalone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "the office address is 10 downing street")
        _add_memory(repo, scope, "the office address is 20 downing street")
        _add_memory(repo, scope, "unrelated fact about the weather")
        scanner = ContradictionScanService(repo)
        pairs = scanner.scan(scope)
        assert len(pairs) == 1  # only the address pair
        # Report-only: nothing mutated.
        assert len(repo.list_memories(scope)) == 3


# --- worker dream modes -----------------------------------------------------


def test_worker_dream_retention_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "old fact for retention", created_delta=timedelta(days=400))
        repo.enqueue_job(scope, "dream", {"mode": "retention", "policy": {"max_age_days": 365}})
        worker = DerivationWorker(repository=repo, derivation=None, projection=proj)  # type: ignore[arg-type]
        summary = worker.drain(gateway=None)  # type: ignore[arg-type]
        assert summary.processed == 1
        # evaluate mode: no mutation.
        assert len(repo.list_memories(scope)) == 1


def test_worker_dream_retention_archive_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope = _scope()
        old = _add_memory(repo, scope, "old fact to archive", created_delta=timedelta(days=400))
        repo.enqueue_job(scope, "dream", {"mode": "retention-archive", "policy": {"max_age_days": 365}})
        worker = DerivationWorker(repository=repo, derivation=None, projection=proj)  # type: ignore[arg-type]
        summary = worker.drain(gateway=None)  # type: ignore[arg-type]
        assert summary.processed == 1
        assert repo.get_memory(scope, old.id).superseded_by_id is not None


def test_worker_dream_contradictions_mode() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "the office address is 10 downing street")
        _add_memory(repo, scope, "the office address is 20 downing street")
        repo.enqueue_job(scope, "dream", {"mode": "contradictions"})
        worker = DerivationWorker(repository=repo, derivation=None, projection=proj)  # type: ignore[arg-type]
        summary = worker.drain(gateway=None)  # type: ignore[arg-type]
        assert summary.processed == 1
        # Scan is report-only.
        assert len(repo.list_memories(scope)) == 2


def test_curation_uses_real_usage_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        unused = _add_memory(repo, scope, "rarely used fact", created_delta=timedelta(hours=1))
        used = _add_memory(repo, scope, "heavily used fact", created_delta=timedelta(hours=1))
        for _ in range(50):
            repo.record_memory_usage(scope, used.id)
        service = CurationService(repo, now=datetime.now(UTC))
        report = service.curate(scope)
        by_id = {entry["memory_id"]: entry for entry in report.scored}
        # Same confidence + same recency, but usage differentiates.
        assert by_id[used.id]["salience"] > by_id[unused.id]["salience"]
