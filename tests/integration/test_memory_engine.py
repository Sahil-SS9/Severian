from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import CloudPolicyError, DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import MemoryService
from severian.domain.errors import ConflictError
from severian.domain.models import Collection, Memory, Scope
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
    # The policy-bearing collection must be provisioned BEFORE the ingest
    # (DATA-05 auto-provisioning would otherwise create a default-policy
    # collection and the explicit policy create would collide).
    repo.create_collection(Collection(id="c", tenant_id="t", name="Primary", policy={"cloud_allowed": False}))
    observation = IngestionService(repo).ingest(scope(), "private", {}).observation
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


# -- P0-5: active-head reads; superseded facts never resurface -----------


def test_list_memories_excludes_superseded_revisions(repo):
    # P0-5 repro: list_memories used to return every record_type memory
    # including superseded heads — stale facts resurfaced by default.
    observation = IngestionService(repo).ingest(scope(), "source", {}).observation
    original = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="old fact",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    revision = repo.revise_memory(
        scope(), original.id, content="new fact", model_id="local/model", prompt_version="v2"
    )

    active = repo.list_memories(scope())
    history = repo.list_memories_history(scope())

    assert [m.id for m in active] == [revision.id]
    assert {m.id for m in history} == {original.id, revision.id}
    assert repo.get_memory(scope(), original.id).superseded_by_id == revision.id


def test_list_memories_orders_by_created_at_not_uuid(repo):
    # E-1 repro: list_memories used to order by record id (UUID) which
    # is random — memory listing was not chronological. Ordering must be
    # by created_at (then id tiebreak) so lists are stable and temporal.
    first_obs = IngestionService(repo).ingest(scope(), "first source", {}).observation
    second_obs = IngestionService(repo).ingest(scope(), "second source", {}).observation
    first = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="first",
            source_observation_ids=[first_obs.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    second = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="second",
            source_observation_ids=[second_obs.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    listed = repo.list_memories(scope())
    assert [m.id for m in listed] == [first.id, second.id]
    assert listed[0].created_at <= listed[1].created_at


def test_list_memory_revisions_returns_full_chain(repo):
    observation = IngestionService(repo).ingest(scope(), "source", {}).observation
    original = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="v1",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="p1",
            confidence=0.8,
        )
    )
    v2 = repo.revise_memory(
        scope(), original.id, content="v2", model_id="local/model", prompt_version="p2"
    )
    v3 = repo.revise_memory(scope(), v2.id, content="v3", model_id="local/model", prompt_version="p3")

    # Naming the middle revision still yields the whole lineage.
    chain = repo.list_memory_revisions(scope(), v2.id)
    assert [m.id for m in chain] == [original.id, v2.id, v3.id]
    assert [m.content for m in chain] == ["v1", "v2", "v3"]


def test_rollback_memory_restores_historical_revision(repo):
    # A-2 (ChronoMem): a bad correction must be reversible. Rolling back
    # to v2 restores it as the active head; the current head (v3) is
    # superseded.
    observation = IngestionService(repo).ingest(scope(), "original claim", {}).observation
    original = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="v1",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="p1",
            confidence=0.8,
        )
    )
    v2 = repo.revise_memory(scope(), original.id, content="v2", model_id="local/model", prompt_version="p2")
    v3 = repo.revise_memory(scope(), v2.id, content="v3", model_id="local/model", prompt_version="p3")

    # v2 is currently superseded (by v3); rollback restores it.
    assert repo.get_memory(scope(), v2.id).superseded_by_id == v3.id
    restored = repo.rollback_memory(scope(), v2.id)

    assert restored.id == v2.id
    assert restored.superseded_by_id is None  # active again
    # The old head (v3) is now superseded by the restored v2.
    assert repo.get_memory(scope(), v3.id).superseded_by_id == v2.id
    # list_memories (active heads) now returns v2, not v3.
    active = [m.id for m in repo.list_memories(scope())]
    assert v2.id in active
    assert v3.id not in active


def test_rollback_memory_rejects_already_active_revision(repo):
    observation = IngestionService(repo).ingest(scope(), "original claim", {}).observation
    original = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="v1",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="p1",
            confidence=0.8,
        )
    )
    with pytest.raises(ConflictError):
        repo.rollback_memory(scope(), original.id)


def test_source_revision_supersedes_derived_memories(repo):
    # P0-5 repro: revising the source observation left memories derived
    # from the OLD head active — zero-stale-facts violated.
    observation = IngestionService(repo).ingest(scope(), "original claim", {}).observation
    repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="stale derived fact",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )

    revision = repo.revise_observation(scope(), observation.id, "corrected claim")
    superseded = repo.supersede_memories_for_source(scope(), observation.id)

    assert revision.content == "corrected claim"
    assert superseded == 1
    # Default reads are active-head only; the stale fact is gone.
    assert repo.list_memories(scope()) == []
    # Explicit history preserves the stale head.
    assert len(repo.list_memories_history(scope())) == 1


class _FailingTargetedProjection:
    """Projection wrapper whose targeted update methods always raise —
    forcing the service's rebuild fallback path (DATA-03)."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def remove_memory(self, memory_id: str) -> None:
        raise RuntimeError("targeted remove_memory failed")

    def index_memory(self, memory: Memory) -> None:
        raise RuntimeError("targeted index_memory failed")


def test_correction_converges_projection_via_rebuild_fallback(repo):
    """DATA-03: if the targeted projection update fails during a
    correction, the service must fall back to a full rebuild so the
    correction is end-to-end durable — search surfaces the corrected
    content, never the stale head."""
    sc = scope()
    # Build real projections.
    fts = FTS5Index(Path("/tmp/severian-data03-fts.db"))
    vec = LocalVectorIndex(Path("/tmp/severian-data03-vec.db"), HashEmbedding())
    coordinator = ProjectionCoordinator(repo, fts, vec)

    # Seed a derived memory and index it.
    observation = IngestionService(repo).ingest(sc, "Ada founded a company", {}).observation
    mem = repo.add_memory(
        Memory(
            scope=sc,
            kind="fact",
            content="Ada founded a company",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    coordinator.rebuild(sc)

    # Correct via the service with a failing targeted projection.
    from severian.application.retrieval import ContextAssembler, RetrievalService
    from severian.application.service import MemoryService

    retrieval_svc = RetrievalService(repo, fts, vec)
    service = MemoryService(
        repo,
        IngestionService(repo),
        retrieval_svc,
        coordinator,
        DerivationService(repo),
        ContextAssembler(),
    )
    service._projection = _FailingTargetedProjection(coordinator)  # type: ignore[assignment]  # force fallback
    revision = service.correct_memory(
        sc, mem.id, content="Ada Lovelace founded a company", model_id="local/model", prompt_version="v2"
    )

    # Canonical: the revision is the active head.
    assert revision.content == "Ada Lovelace founded a company"
    assert [m.id for m in repo.list_memories(sc)] == [revision.id]

    # Projection converged via rebuild: search returns the corrected fact.
    hits = retrieval_svc.search(sc, "Ada Lovelace founded", limit=10)
    assert hits, "corrected memory must be searchable after rebuild fallback"
    assert hits[0].memory.id == revision.id


def test_erase_memory_tombstones_from_active_reads_and_search(repo):
    """OPS-02: erase_memory removes a memory from active reads and search
    (tombstone semantics) while history preserves the record for audit."""
    sc = scope()
    fts = FTS5Index(Path("/tmp/severian-ops02-fts.db"))
    vec = LocalVectorIndex(Path("/tmp/severian-ops02-vec.db"), HashEmbedding())
    coordinator = ProjectionCoordinator(repo, fts, vec)

    observation = IngestionService(repo).ingest(sc, "Ada founded a company", {}).observation
    mem = repo.add_memory(
        Memory(
            scope=sc,
            kind="fact",
            content="Ada founded a company",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    coordinator.rebuild(sc)

    service = MemoryService(
        repo,
        IngestionService(repo),
        RetrievalService(repo, fts, vec),
        coordinator,
        DerivationService(repo),
        ContextAssembler(),
    )

    erased = service.erase_memory(sc, mem.id, reason="user-requested")

    # Active reads: the memory is gone.
    assert repo.list_memories(sc) == []
    # erased is the ORIGINAL marked superseded; its superseded_by_id
    # points at the tombstone revision (a distinct record).
    assert erased.id == mem.id
    tombstone = repo.get_memory(sc, erased.superseded_by_id or "")
    assert tombstone.id != mem.id
    assert tombstone.content == "[user-requested]"
    assert tombstone.revision_of_id == mem.id
    # Search: the memory no longer surfaces.
    hits = RetrievalService(repo, fts, vec).search(sc, "Ada founded", limit=10)
    assert hits == []
    # History: the record AND the tombstone are retained for audit.
    history = repo.list_memories_history(sc)
    assert {m.id for m in history} == {mem.id, tombstone.id}


def test_erase_observation_propagates_to_derived_memories(repo):
    """OPS-02: erase_observation tombstones the source AND erases every
    active memory derived from it — search stops returning facts whose
    only source was erased."""
    sc = scope()
    fts = FTS5Index(Path("/tmp/severian-ops02b-fts.db"))
    vec = LocalVectorIndex(Path("/tmp/severian-ops02b-vec.db"), HashEmbedding())
    coordinator = ProjectionCoordinator(repo, fts, vec)

    result = IngestionService(repo).ingest(sc, "Grace invented COBOL", {})
    observation = result.observation
    repo.add_memory(
        Memory(
            scope=sc,
            kind="fact",
            content="Grace invented COBOL",
            source_observation_ids=[observation.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    coordinator.rebuild(sc)

    service = MemoryService(
        repo,
        IngestionService(repo),
        RetrievalService(repo, fts, vec),
        coordinator,
        DerivationService(repo),
        ContextAssembler(),
    )

    service.erase_observation(sc, observation.id, reason="source-withdrawn")

    # Observation tombstoned, derived memory erased, search empty.
    assert repo.get_observation(sc, observation.id).superseded_by_id is not None
    assert repo.list_memories(sc) == []
    hits = RetrievalService(repo, fts, vec).search(sc, "Grace COBOL", limit=10)
    assert hits == []
    # History intact for audit.
    assert len(repo.list_memories_history(sc)) == 2
