"""Tests for episodic summarization (B-2 / roadmap technique #4).

Proves the episode consolidation path: a session with enough
observations produces a ``Memory(kind="episode")`` spanning them all,
the deterministic summarizer is reproducible, episodes are searchable,
and the worker's ``episode`` job kind closes the queue loop.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.episodes import (
    MIN_OBSERVATIONS_FOR_EPISODE,
    EpisodeService,
    summarize_observations,
)
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.application.worker import DerivationWorker
from severian.domain.models import Collection, Scope
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


@dataclass
class SummarizingGateway(FakeGateway):
    """Gateway with a summarize capability (LLM-judge path)."""

    def summarize(self, content: str, *, policy: dict[str, object]) -> str:
        return f"EPISODE-SUMMARY: {content[:80]}"


def _scope(tenant: str = "t1") -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id="p1",
        collection_id="c1",
        agent_id="a1",
        session_id="s1",
    )


@pytest.fixture
def bundle(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts.db")
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    derivation = DerivationService(repo)
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=ProjectionCoordinator(repo, fts, vector),
        derivation=derivation,
        context=ContextAssembler(),
    )
    worker = DerivationWorker(repo, derivation, ProjectionCoordinator(repo, fts, vector))
    yield {"repo": repo, "service": service, "worker": worker, "fts": fts, "vector": vector, "gateway": FakeGateway()}
    fts.close()
    vector.close()
    repo.close()


def _ingest_session(bundle, count: int, *, scope: Scope | None = None) -> list[str]:
    sc = scope or _scope()
    ids: list[str] = []
    for i in range(count):
        view = bundle["service"].ingest_observation(
            sc, content=f"Observation {i}: the search index is SQLite FTS5.", source={"uri": f"doc://{i}"}
        )
        ids.append(view.observation_id)
    return ids


def test_summarize_observations_is_deterministic_and_budgeted() -> None:
    from severian.domain.models import Observation

    obs = [
        Observation(scope=_scope(), content=f"fact number {i}", raw_content="r", raw_hash="0" * 64)
        for i in range(10)
    ]
    first = summarize_observations(obs)
    second = summarize_observations(obs)
    assert first == second  # reproducible
    assert len(first) <= 4000  # budget respected
    assert "fact number 0" in first


def test_consolidate_session_requires_minimum_observations(bundle):
    sc = _scope()
    _ingest_session(bundle, 2, scope=sc)  # below MIN_OBSERVATIONS_FOR_EPISODE
    episodes = EpisodeService(bundle["repo"])
    result = episodes.consolidate_session(sc)
    assert result is None


def test_consolidate_session_builds_episode_spanning_all_observations(bundle):
    sc = _scope()
    ids = _ingest_session(bundle, MIN_OBSERVATIONS_FOR_EPISODE + 2, scope=sc)
    episodes = EpisodeService(bundle["repo"])
    episode = episodes.consolidate_session(sc)
    assert episode is not None
    assert episode.kind == "episode"
    assert set(episode.source_observation_ids) == set(ids)
    assert all(obs_id in episode.source_observation_ids for obs_id in ids)
    assert episode.model_id == "severian/deterministic-episode"
    assert episode.content  # non-empty deterministic summary


def test_worker_processes_episode_job_kind(bundle):
    sc = _scope()
    _ingest_session(bundle, MIN_OBSERVATIONS_FOR_EPISODE + 1, scope=sc)

    # Enqueue an episode job directly (the API layer will do this in
    # production; here we prove the worker's episode gate).
    bundle["repo"].enqueue_job(sc, kind="episode", payload={})

    summary = bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])
    # derive jobs (one per observation) + 1 episode job
    assert summary.processed == MIN_OBSERVATIONS_FOR_EPISODE + 1 + 1
    assert summary.failed == 0

    episodes = bundle["repo"].list_memories(sc)
    episode_memories = [m for m in episodes if m.kind == "episode"]
    assert len(episode_memories) == 1
    assert len(episode_memories[0].source_observation_ids) == MIN_OBSERVATIONS_FOR_EPISODE + 1


def test_episode_is_searchable_after_worker_drain(bundle):
    sc = _scope()
    _ingest_session(bundle, MIN_OBSERVATIONS_FOR_EPISODE + 1, scope=sc)
    bundle["repo"].enqueue_job(sc, kind="episode", payload={})
    bundle["worker"].drain(worker_id="w1", gateway=bundle["gateway"])

    hits = bundle["service"].search(sc, query="search index", limit=10)
    # Episode memory contains "the search index is SQLite FTS5" repeated
    # observations, so a lexical query should surface it.
    assert any("SQLite FTS5" in hit.content for hit in hits)


def test_episode_uses_gateway_summarize_with_fallback(bundle):
    sc = _scope()
    _ingest_session(bundle, MIN_OBSERVATIONS_FOR_EPISODE + 1, scope=sc)

    episodes = EpisodeService(bundle["repo"])
    with_gateway = episodes.consolidate_session(sc, gateway=SummarizingGateway())
    assert with_gateway is not None
    assert with_gateway.content.startswith("EPISODE-SUMMARY:")

    # Failing gateway falls back to deterministic summary (never empty).
    class BrokenSummarize(SummarizingGateway):
        def summarize(self, content: str, *, policy: dict[str, object]) -> str:
            raise RuntimeError("summarize boom")

    without = episodes.consolidate_session(sc, gateway=BrokenSummarize())
    assert without is not None
    assert without.content  # fallback populated
