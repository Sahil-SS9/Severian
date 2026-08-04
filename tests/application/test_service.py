"""Tests for the canonical application-facing MemoryService.

These tests enforce the Phase 3 rule that API/CLI/SDK never write
to the repository or projection indexes directly. They must go
through ``MemoryService`` which composes the canonical Phase 1+2
building blocks.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import (
    BuildMemoryService,
    ObservationView,
    SearchHitView,
)
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import NotFoundError, SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _scope(tenant: str = "t1", profile: str = "p1") -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id=profile,
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
    yield {"repo": repo, "service": service, "fts": fts, "vector": vector, "gateway": FakeGateway()}
    fts.close()
    vector.close()
    repo.close()


def test_ingest_returns_observation_view_and_enqueues_derivation(bundle):
    view = bundle["service"].ingest_observation(
        _scope(),
        content="  Hello\r\nworld  ",
        source={"uri": "note://1"},
    )

    assert isinstance(view, ObservationView)
    assert view.content == "Hello\nworld"
    assert view.content_hash  # populated
    assert view.job_kind == "derive"
    assert view.observation_id
    assert view.observation_id == bundle["service"].get_observation(_scope(), view.observation_id).id


def test_ingest_duplicate_returns_existing_observation(bundle):
    first = bundle["service"].ingest_observation(_scope(), content="Hello", source={"uri": "note://1"})
    second = bundle["service"].ingest_observation(_scope(), content="Hello", source={"uri": "note://1"})

    assert second.observation_id == first.observation_id
    assert second.job_id == first.job_id


def test_ingest_rejects_empty_content(bundle):
    with pytest.raises(ValueError, match="empty"):
        bundle["service"].ingest_observation(_scope(), content=" \n\t", source={})


def test_search_returns_scope_bounded_results(bundle):
    p1_obs = bundle["service"].ingest_observation(_scope("t1", "p1"), content="Ada founded a company", source={})
    p2_obs = bundle["service"].ingest_observation(_scope("t1", "p2"), content="Grace Hopper invented COBOL", source={})

    p1_hits = bundle["service"].search(
        _scope("t1", "p1"), query="Ada", limit=5, derive=True, gateway=bundle["gateway"]
    )
    assert p1_hits
    assert all(hit.scope.profile_id == "p1" for hit in p1_hits)
    assert all(p1_obs.observation_id in hit.source_observation_ids for hit in p1_hits)

    p2_hits = bundle["service"].search(
        _scope("t1", "p2"), query="Grace", limit=5, derive=True, gateway=bundle["gateway"]
    )
    assert p2_hits
    assert all(hit.scope.profile_id == "p2" for hit in p2_hits)
    assert all(p2_obs.observation_id in hit.source_observation_ids for hit in p2_hits)

    # The two scopes must never share memory IDs.
    p1_ids = {hit.memory_id for hit in p1_hits}
    p2_ids = {hit.memory_id for hit in p2_hits}
    assert p1_ids.isdisjoint(p2_ids)


def test_search_with_derive_populates_indexes_through_projection(bundle):
    observation = bundle["service"].ingest_observation(_scope(), content="Ada founded a company", source={})
    hits = bundle["service"].search(_scope(), query="Ada founded", limit=5, derive=True, gateway=bundle["gateway"])
    assert hits, "derive=True should produce at least one indexed memory"
    assert all(isinstance(hit, SearchHitView) for hit in hits)
    assert hits[0].source_observation_ids == [observation.observation_id]


def test_rebuild_is_idempotent_when_canonical_unchanged(bundle):
    # A-2 second-order fix: repeated rebuilds with unchanged canonical
    # data must skip (no clear+reinsert churn). The digest cache makes
    # the second rebuild a no-op; the projections stay consistent.
    from severian.application.projections import ProjectionCoordinator

    service = bundle["service"]
    projection = ProjectionCoordinator(bundle["repo"], bundle["fts"], bundle["vector"])
    service.ingest_observation(_scope(), content="Ada founded a company", source={})
    service.search(_scope(), query="Ada founded", limit=5, derive=True, gateway=bundle["gateway"])

    status_before = projection.status(_scope())
    # Second rebuild: nothing changed, so it must be a no-op (same digests).
    projection.rebuild(_scope())
    status_after = projection.status(_scope())
    assert status_after.in_sync
    assert status_after.canonical_digest == status_before.canonical_digest
    assert status_after.vector_digest == status_before.vector_digest
    assert status_after.lexical_digest == status_before.lexical_digest

    # New observation -> digest changes -> rebuild actually refreshes.
    service.ingest_observation(_scope(), content="Grace wrote the first compiler", source={})
    service.search(_scope(), query="Grace", limit=5, derive=True, gateway=bundle["gateway"])
    projection.rebuild(_scope())
    status_new = projection.status(_scope())
    assert status_new.canonical_digest != status_before.canonical_digest
    assert status_new.in_sync


def test_get_observation_rejects_cross_scope_access(bundle):
    observation = bundle["service"].ingest_observation(_scope("t1", "p1"), content="secret", source={})
    with pytest.raises(NotFoundError):
        bundle["service"].get_observation(_scope("t1", "p2"), observation.observation_id)
    with pytest.raises(NotFoundError):
        bundle["service"].get_observation(_scope("t2", "p1"), observation.observation_id)


def test_assemble_context_cites_sources(bundle):
    bundle["service"].ingest_observation(_scope(), content="Ada founded a company", source={})
    hits = bundle["service"].search(_scope(), query="Ada", limit=5, derive=True, gateway=bundle["gateway"])
    context = bundle["service"].assemble_context(_scope(), hits, max_chars=2000)
    assert "source:" in context
    assert hits[0].source_observation_ids[0] in context


def test_assemble_context_rejects_cross_scope_hits(bundle):
    bundle["service"].ingest_observation(_scope("t1", "p1"), content="Ada founded a company", source={})
    hits = bundle["service"].search(_scope("t1", "p1"), query="Ada", limit=5, derive=True, gateway=bundle["gateway"])
    with pytest.raises(NotFoundError):
        bundle["service"].assemble_context(_scope("t1", "p2"), hits)


def test_service_health_reports_components(bundle):
    health = bundle["service"].health()
    assert health.ready is True
    assert health.components["repository"] == "ok"
    assert health.components["lexical"] == "ok"
    assert health.components["vector"] == "ok"
