from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import CloudPolicyError, DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
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


def test_derivation_rejects_malformed_candidates_without_partial_publication(repo):
    """P0-4: a malformed candidate aborts the whole batch — no partial
    publication of the valid candidates that preceded it."""
    observation = IngestionService(repo).ingest(scope(), "private", {}).observation

    class MalformedGateway:
        model_id = "local/test-model"
        prompt_version = "prompt-v1"
        is_cloud = False

        def derive(self, content: str, *, policy: dict[str, object]):  # type: ignore[no-untyped-def]
            return [{"kind": "fact", "content": "valid"}, None, {"kind": "fact", "content": "also-valid"}]

    with pytest.raises(ValueError, match="malformed derivation candidate"):
        DerivationService(repo).derive_observation(scope(), observation.id, MalformedGateway(), policy={})

    # Nothing was published — the valid candidate that appeared before
    # the malformed one must not have been written.
    assert repo.list_memories(scope()) == []


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


# -- P0-1: source evidence preservation -----------------------------------


def test_ingestion_preserves_raw_evidence_alongside_normalised_content(repo):
    result = IngestionService(repo).ingest(scope(), "  Hello\r\nWorld  ", {"uri": "note://1"})

    assert result.observation.content == "Hello\nWorld"
    assert result.observation.raw_content == "  Hello\r\nWorld  "
    assert result.observation.raw_hash
    stored = repo.list_observations(scope())[0]
    assert stored.raw_content == "  Hello\r\nWorld  "
    assert stored.raw_hash == result.observation.raw_hash


def test_ingestion_keeps_distinct_sources_separate_when_normalised_collides(repo):
    # P0-1 repro: ``①`` from note://A and ``1`` from note://B normalise to
    # the same content but are DIFFERENT evidence. They must not collapse
    # into one record.
    first = IngestionService(repo).ingest(scope(), "①", {"uri": "note://A"})
    second = IngestionService(repo).ingest(scope(), "1", {"uri": "note://B"})

    assert first.observation.id != second.observation.id
    assert first.observation.content == second.observation.content == "1"
    records = repo.list_observations(scope())
    assert len(records) == 2
    assert {record.raw_content for record in records} == {"①", "1"}


def test_ingestion_same_source_same_raw_evidence_dedupes(repo):
    first = IngestionService(repo).ingest(scope(), "same", {"uri": "note://A"})
    second = IngestionService(repo).ingest(scope(), "same", {"uri": "note://A"})

    assert first.observation.id == second.observation.id
    assert len(repo.list_observations(scope())) == 1


# -- P0-2: cloud policy is deny-dominant and provenance is trusted ---------


def test_cloud_derivation_cannot_be_enabled_by_caller_policy(repo):
    # P0-2 repro: stored policy denies cloud; caller passing
    # ``cloud_allowed: True`` must NOT flip the decision.
    observation = IngestionService(repo).ingest(scope(), "private", {}).observation
    repo.create_collection(Collection(id="c", tenant_id="t", name="Primary", policy={"cloud_allowed": False}))
    cloud = FakeGateway(model_id="cloud/model", is_cloud=True)

    with pytest.raises(CloudPolicyError):
        DerivationService(repo).derive_observation(
            scope(), observation.id, cloud, policy={"cloud_allowed": True}
        )


def test_derivation_provenance_comes_from_gateway_not_candidate(repo):
    # P0-2 repro: a model-produced candidate must not forge model_id /
    # prompt_version. Provenance is taken from the trusted gateway.
    observation = IngestionService(repo).ingest(scope(), "private", {}).observation

    class ForgingGateway:
        model_id = "trusted/model"
        prompt_version = "trusted-prompt"
        is_cloud = False

        def derive(self, content: str, *, policy: dict[str, object]):
            return [{"kind": "fact", "content": "derived", "model_id": "forged/model", "prompt_version": "forged"}]

    memories = DerivationService(repo).derive_observation(
        scope(), observation.id, ForgingGateway(), policy={}
    )
    assert len(memories) == 1
    assert memories[0].model_id == "trusted/model"
    assert memories[0].prompt_version == "trusted-prompt"
