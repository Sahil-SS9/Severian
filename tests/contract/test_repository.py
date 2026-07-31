from datetime import UTC, datetime

import pytest

from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.sqlite import ConflictError, SQLiteRepository


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteRepository(tmp_path / "severian.db")
    repository.initialize()
    yield repository
    repository.close()


def scope(profile="p1"):
    return Scope(tenant_id="t1", profile_id=profile, collection_id="c1", agent_id="a1", session_id="s1")


def test_collection_and_observation_ingestion_is_idempotent(repo):
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary", policy={"cloud_allowed": False}))
    obs = Observation(scope=scope(), content="Original evidence", source={"uri": "file:///note"})
    first = repo.add_observation(obs)
    second = repo.add_observation(obs.model_copy(update={"id": "different"}))
    assert first.id == second.id
    assert first.content_hash == second.content_hash


def test_scope_isolation(repo):
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    first = repo.add_observation(Observation(scope=scope("p1"), content="same"))
    second = repo.add_observation(Observation(scope=scope("p2"), content="same"))
    assert first.id != second.id
    assert [o.content for o in repo.list_observations(scope("p1"))] == ["same"]


def test_original_observation_is_immutable_and_correction_is_revision(repo):
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    original = repo.add_observation(Observation(scope=scope(), content="old"))
    with pytest.raises(ConflictError):
        repo.update_observation_content(original.id, "new")
    revision = repo.revise_observation(scope(), original.id, "new")
    assert revision.revision_of_id == original.id
    assert repo.get_observation(scope(), original.id).content == "old"
    assert repo.get_observation(scope(), original.id).superseded_by_id == revision.id


def test_memory_requires_and_roundtrips_lineage(repo):
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    observation = repo.add_observation(Observation(scope=scope(), content="evidence"))
    memory = Memory(
        scope=scope(),
        kind="fact",
        content="derived",
        source_observation_ids=[observation.id],
        model_id="local/model",
        prompt_version="v1",
        confidence=0.8,
    )
    saved = repo.add_memory(memory)
    assert repo.get_memory(scope(), saved.id) == saved


def test_entities_temporal_relations_jobs_outbox_and_audit_are_durable(repo):
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    person = repo.add_entity(scope(), "person", "Ada")
    org = repo.add_entity(scope(), "organisation", "Analytical Engines")
    relation = repo.add_relation(scope(), person.id, "founded", org.id, valid_from=datetime(1843, 1, 1, tzinfo=UTC))
    job = repo.enqueue_job(scope(), "derive", {"observation_id": "o1"})
    claimed = repo.claim_job("worker-1")
    event = repo.append_outbox(scope(), "memory.created", {"id": "m1"})
    audit = repo.append_audit(scope(), "observation.create", "o1", {"source": "test"})
    assert relation.valid_from.year == 1843
    assert claimed.id == job.id and claimed.status == "running"
    assert event.id in {item.id for item in repo.pending_outbox()}
    assert audit.id in {item.id for item in repo.list_audit(scope())}
