"""Regression tests for the 2026-08-05 architecture-audit repair batch.

Each test is intentionally focused and RED before its fix:
- graph token-membership (not substring) for related_memories
- shared-retrieval min_coverage abstention
- contradiction detector abstraction (single helper, same results)
- duplicate grouping still finds known duplicates (LSH-framing safe)
- worker derive-without-derivation raises a clear error
- retention marker does not fabricate observation IDs
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from severian.application.dreaming import (
    ConsolidationService,
    ContradictionScanService,
    detect_contradictions,
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


def _scope(agent: str = "a1") -> Scope:
    return Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1", agent_id=agent, session_id="s1"
    )


def _add_memory(repo, scope, content, *, created_delta=None, confidence=0.9):
    import uuid

    obs = repo.add_observation(
        Observation(
            scope=scope, content=content, raw_content=content,
            source={"uri": f"note://{uuid.uuid4().hex[:12]}"},
        )
    )
    memory = Memory(
        scope=scope, kind="fact", content=content,
        source_observation_ids=[obs.id], model_id="test", prompt_version="p1",
        confidence=confidence,
    )
    if created_delta is not None:
        memory = memory.model_copy(update={"created_at": datetime.now(UTC) - created_delta})
    return repo.add_memory(memory)


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


# --- 1. graph token-membership ------------------------------------------------


def test_graph_related_memories_token_membership_not_substring() -> None:
    """Entity 'Al' must not match memory content 'album' (substring bug).

    A memory mentioning the real token 'Al' must match; a memory that merely
    contains 'album' (substring containment) must NOT.
    """
    from severian.application.graph import GraphRetrievalService

    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        al = repo.add_entity(scope, "person", "Al")
        repo.add_entity(scope, "thing", "Album")  # noqa: F841 — entity exists to ensure substring "album" does not match "Al"
        # memory about an album (substring 'al' in 'album') — must NOT match Al
        _add_memory(repo, scope, "the photo album has wedding pictures")
        # memory that genuinely mentions the token Al — must match
        _add_memory(repo, scope, "Al presented the quarterly results")
        graph = GraphRetrievalService(repo)
        related = graph.related_memories(scope, al.id, depth=1)
        contents = {r.memory.content for r in related}
        assert "Al presented the quarterly results" in contents
        assert "the photo album has wedding pictures" not in contents


def test_graph_related_memories_token_membership_preserves_real_match() -> None:
    """A real entity token present as a standalone token in content matches."""
    from severian.application.graph import GraphRetrievalService

    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        bob = repo.add_entity(scope, "person", "Bob")
        _add_memory(repo, scope, "Bob and Alice reviewed the merge request")
        graph = GraphRetrievalService(repo)
        related = graph.related_memories(scope, bob.id, depth=1)
        assert len(related) == 1
        assert "Bob" in related[0].memory.content


# --- 2. shared retrieval min_coverage abstention ------------------------------


def test_search_shared_min_coverage_filters_lexical_decoy() -> None:
    """A lexical decoy shared across agent scopes is filtered by min_coverage."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope_a = _scope("a1")
        scope_b = _scope("a2")
        # genuine evidence in agent a1
        _add_memory(repo, scope_a, "Project Chrysalis launches on 2026-09-01")
        # lexical decoy in agent a2 (shares only the word 'project')
        _add_memory(repo, scope_b, "the old project lead was a software engineer")
        proj.rebuild(scope_a)
        proj.rebuild(scope_b)
        retrieval = RetrievalService(repo, fts, vec)

        # without min_coverage, the decoy may appear in shared results
        no_gate = retrieval.search_shared(scope_a, ["a2"], "Project Chrysalis launch", limit=10)
        decoy_present = any(
            "old project lead" in r.memory.content for r in no_gate
        )

        # with min_coverage, the decoy must be filtered out
        gated = retrieval.search_shared(
            scope_a, ["a2"], "Project Chrysalis launch", limit=10, min_coverage=0.4
        )
        gated_contents = {r.memory.content for r in gated}
        assert "the old project lead was a software engineer" not in gated_contents
        # genuine evidence survives the gate
        assert "Project Chrysalis launches on 2026-09-01" in gated_contents
        # if the decoy was present without the gate, the gate demonstrably filtered it
        if decoy_present:
            assert len(gated) < len(no_gate)


# --- 3. contradiction detector abstraction ------------------------------------


def test_detect_contradictions_helper_matches_consolidation() -> None:
    """The shared detect_contradictions helper returns the same pairs as the
    ConsolidationService and ContradictionScanService (single source of truth).
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "the office address is 10 downing street")
        _add_memory(repo, scope, "the office address is 20 downing street")
        _add_memory(repo, scope, "the revenue target for q3 is twelve million")
        _add_memory(repo, scope, "the revenue target for q4 is fifteen million")
        memories = repo.list_memories(scope)

        standalone = ContradictionScanService(repo).scan(scope)
        consolidated = ConsolidationService(repo)._find_contradictions(memories)
        shared = detect_contradictions(memories)

        standalone_ids = {(c.memory_a_id, c.memory_b_id) for c in standalone}
        consolidated_ids = {(a, b) for a, b, _ in consolidated}
        shared_ids = {(a, b) for a, b, _ in shared}
        assert standalone_ids == shared_ids
        assert consolidated_ids == shared_ids


# --- 4. duplicate grouping still finds known duplicates -----------------------


def test_consolidation_duplicate_grouping_finds_known_duplicates() -> None:
    """After the detector abstraction, the duplicate merge still finds
    a pair of known near-duplicate memories (exact same content).
    """
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        older = _add_memory(
            repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=10)
        )
        newer = _add_memory(
            repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=1)
        )
        service = ConsolidationService(repo)
        report = service.consolidate(scope)
        assert len(report.duplicate_groups) == 1
        group = report.duplicate_groups[0]
        assert group.survivor_id == newer.id
        assert group.superseded_ids == (older.id,)


# --- 5. worker derive-without-derivation raises clear error -------------------


def test_worker_derive_job_without_derivation_raises_clear_error() -> None:
    """A derive job arriving when derivation is None must raise a clear,
    explicit error (not a bare AttributeError on NoneType).
    """
    from severian.domain.models import Job

    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope = _scope()
        worker = DerivationWorker(repository=repo, derivation=None, projection=proj)
        job = Job(scope=scope, kind="derive", payload={"observation_id": "obs-x"})
        with pytest.raises(RuntimeError, match="derivation"):
            worker._derive(job, gateway=FakeGateway(), policy=None)


def test_worker_dream_only_without_derivation_succeeds() -> None:
    """A dream-only worker (derivation=None) must still process dream jobs."""
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope = _scope()
        _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=10))
        _add_memory(repo, scope, "the search index uses sqlite fts5", created_delta=timedelta(days=1))
        repo.enqueue_job(scope, "dream", {"mode": "consolidate"})
        worker = DerivationWorker(repository=repo, derivation=None, projection=proj)
        summary = worker.drain(gateway=FakeGateway())
        assert summary.processed == 1
        assert summary.failed == 0


# --- 6. retention marker does not fabricate observation IDs -------------------


def test_retention_archive_marker_uses_real_observation_ids() -> None:
    """The retention archive marker must source its observation IDs from the
    archived memories' real provenance, never fabricate IDs.
    """
    from severian.application.dreaming import RetentionPolicyService

    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        m1 = _add_memory(repo, scope, "stale fact one", created_delta=timedelta(days=400))
        m2 = _add_memory(repo, scope, "stale fact two", created_delta=timedelta(days=400))
        service = RetentionPolicyService(repo)
        service.apply_archive(scope, {"max_age_days": 30})
        # marker memory written with real source observation IDs
        markers = [m for m in repo.list_memories(scope) if m.kind == "retention_archive"]
        assert len(markers) == 1
        marker = markers[0]
        real_obs_ids = {m1.source_observation_ids[0], m2.source_observation_ids[0]}
        assert set(marker.source_observation_ids).issubset(real_obs_ids)
        # no fabricated observation IDs (every ID must be a real observation)
        for obs_id in marker.source_observation_ids:
            repo.get_observation(scope, obs_id)  # raises if fabricated
