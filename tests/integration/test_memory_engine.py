from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import CloudPolicyError, DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.domain.models import Scope
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


def scope() -> Scope:
    return Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")


@pytest.fixture
def repo(tmp_path: Path):
    repository = SQLiteRepository(tmp_path / "severian.db")
    repository.initialize()
    yield repository
    repository.close()


def test_ingestion_normalises_content_and_enqueues_derivation(repo):
    service = IngestionService(repo)
    result = service.ingest(scope(), "  Hello\r\nworld  ", {"uri": "note://1"})
    duplicate = service.ingest(scope(), "Hello\nworld", {"uri": "note://1"})

    assert result.observation.content == "Hello\nworld"
    assert result.job.kind == "derive"
    assert result.job.payload == {"observation_id": result.observation.id}
    assert duplicate.observation.id == result.observation.id
    assert duplicate.job.id == result.job.id
    assert repo.list_observations(scope())[0].content == "Hello\nworld"


def test_ingestion_rejects_empty_content(repo):
    with pytest.raises(ValueError, match="empty"):
        IngestionService(repo).ingest(scope(), " \n\t", {})


def test_job_identity_is_stable_for_equivalent_mapping_order(repo):
    first = repo.enqueue_job(scope(), "derive", {"a": 1, "b": 2})
    second = repo.enqueue_job(scope(), "derive", {"b": 2, "a": 1})

    assert second.id == first.id


def test_derivation_records_model_prompt_and_provenance(repo):
    observation = IngestionService(repo).ingest(scope(), "Ada founded a company", {}).observation
    memories = DerivationService(repo).derive_observation(scope(), observation.id, FakeGateway(), policy={})

    assert len(memories) == 1
    assert memories[0].source_observation_ids == [observation.id]
    assert memories[0].model_id == "local/test-model"
    assert memories[0].prompt_version == "prompt-v1"
    retry = DerivationService(repo).derive_observation(scope(), observation.id, FakeGateway(), policy={})
    assert [item.id for item in retry] == [memories[0].id]


def test_cloud_derivation_is_denied_by_collection_policy(repo):
    observation = IngestionService(repo).ingest(scope(), "private", {}).observation
    cloud = FakeGateway(model_id="cloud/model", is_cloud=True)

    with pytest.raises(CloudPolicyError):
        DerivationService(repo).derive_observation(
            scope(), observation.id, cloud, policy={"cloud_allowed": False}
        )


def test_derivation_skips_malformed_gateway_candidates(repo):
    observation = IngestionService(repo).ingest(scope(), "private", {}).observation

    class MalformedGateway:
        model_id = "local/test-model"
        prompt_version = "prompt-v1"
        is_cloud = False

        def derive(self, content: str, *, policy: dict[str, object]):  # type: ignore[no-untyped-def]
            return [None, "not-a-candidate", 42, {"kind": "fact", "content": "valid"}]

    memories = DerivationService(repo).derive_observation(scope(), observation.id, MalformedGateway(), policy={})

    assert [memory.content for memory in memories] == ["valid"]


def test_fts_projection_is_scope_aware_and_rebuildable(repo, tmp_path: Path):
    observation = IngestionService(repo).ingest(scope(), "Ada founded a company", {}).observation
    memory = DerivationService(repo).derive_observation(scope(), observation.id, FakeGateway(), policy={})[0]
    index = FTS5Index(tmp_path / "fts.db")
    index.upsert(memory)

    hits = index.search(scope(), "Fact Ada", limit=5)
    assert [hit.memory_id for hit in hits] == [memory.id]
    other_scope = Scope(tenant_id="other", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    assert index.search(other_scope, "Ada") == []

    index.clear()
    assert index.search(scope(), "Ada") == []
    index.rebuild(repo, scope())
    assert [hit.memory_id for hit in index.search(scope(), "Ada")] == [memory.id]
    index.close()


def test_local_vector_projection_is_rebuildable_and_scope_aware(repo, tmp_path: Path):
    observation = IngestionService(repo).ingest(scope(), "Ada founded a company", {}).observation
    memory = DerivationService(repo).derive_observation(scope(), observation.id, FakeGateway(), policy={})[0]
    index = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    index.upsert(memory)

    assert index.search(scope(), "Ada founded", limit=5)[0].memory_id == memory.id
    index.clear()
    index.rebuild(repo, scope())
    assert index.search(scope(), "Ada founded", limit=5)[0].memory_id == memory.id
    index.close()


def test_hybrid_retrieval_and_context_assembly_cite_sources(repo, tmp_path: Path):
    observation = IngestionService(repo).ingest(scope(), "Ada founded a company", {"uri": "note://1"}).observation
    memory = DerivationService(repo).derive_observation(scope(), observation.id, FakeGateway(), policy={})[0]
    fts = FTS5Index(tmp_path / "fts.db")
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    fts.rebuild(repo, scope())
    vector.rebuild(repo, scope())

    results = RetrievalService(repo, fts, vector).search(scope(), "Ada founded", limit=5)
    assert results[0].memory.id == memory.id
    assert results[0].source_observation_ids == [observation.id]
    # Empty/whitespace query is the recall-all path (admin graph `q=*`).
    assert RetrievalService(repo, fts, vector).search(scope(), " \t\n")[0].memory.id == memory.id
    # Non-searchable garbage still returns nothing.
    assert RetrievalService(repo, fts, vector).search(scope(), "\x00***") == []
    context = ContextAssembler(max_chars=1000).assemble(results)
    assert "source:" in context and observation.id in context
    fts.close()
    vector.close()


def test_fts_query_sanitises_null_bytes_and_operators(repo, tmp_path: Path):
    observation = IngestionService(repo).ingest(scope(), "Ada founded a company", {}).observation
    memory = DerivationService(repo).derive_observation(scope(), observation.id, FakeGateway(), policy={})[0]
    index = FTS5Index(tmp_path / "fts.db")
    index.upsert(memory)

    assert index.search(scope(), "\x00***") == []
    assert index.search(scope(), 'Ada OR "company"')
    index.close()


def test_projection_coordinator_tracks_lag_repairs_and_deletes(repo, tmp_path: Path):
    observation = IngestionService(repo).ingest(scope(), "Ada founded a company", {}).observation
    memory = DerivationService(repo).derive_observation(scope(), observation.id, FakeGateway(), policy={})[0]
    fts = FTS5Index(tmp_path / "fts.db")
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    coordinator = ProjectionCoordinator(repo, fts, vector)

    assert coordinator.status(scope()).in_sync is False
    coordinator.index_memory(memory)
    assert coordinator.status(scope()).in_sync is True
    coordinator.remove_memory(memory.id)
    assert coordinator.status(scope()).in_sync is False
    coordinator.rebuild(scope())
    assert coordinator.status(scope()).in_sync is True
    fts.close()
    vector.close()
