"""Tests for Tier 3 AI-memory procedures (2026-08-05).

Covers:
- graph retrieval: entity/relation list methods, neighborhood traversal
  (BFS, both directions, temporal validity filtering), related_memories
- multi-modal ingestion: Attachment model validation, ingest with
  attachments, digest validation, scope-bound storage
- shared-scope visibility: search_shared merges agent scopes
  deterministically, does NOT collapse identity, refuses empty agent set
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from severian.application.graph import GraphRetrievalService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import RetrievalService
from severian.domain.models import (
    Attachment,
    Collection,
    Memory,
    Observation,
    Scope,
)
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


def _add_memory(repo: SQLiteRepository, scope: Scope, content: str) -> Memory:
    import uuid

    obs = repo.add_observation(
        Observation(
            scope=scope, content=content, raw_content=content,
            source={"uri": f"note://{uuid.uuid4().hex[:12]}"},
        )
    )
    return repo.add_memory(
        Memory(
            scope=scope, kind="fact", content=content,
            source_observation_ids=[obs.id], model_id="test", prompt_version="p1",
            confidence=0.9,
        )
    )


# --- graph retrieval --------------------------------------------------------


def test_graph_neighborhood_traverses_both_directions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        alice = repo.add_entity(scope, "person", "Alice")
        bob = repo.add_entity(scope, "person", "Bob")
        carol = repo.add_entity(scope, "person", "Carol")
        repo.add_relation(scope, alice.id, "works_with", bob.id)
        repo.add_relation(scope, bob.id, "works_with", carol.id)

        graph = GraphRetrievalService(repo)
        n1 = graph.neighborhood(scope, alice.id, depth=1)
        assert bob.id in n1.entity_ids()
        assert carol.id not in n1.entity_ids()  # 2 hops away

        n2 = graph.neighborhood(scope, alice.id, depth=2)
        assert carol.id in n2.entity_ids()
        assert len(n2.relations) == 2
        # Deterministic ordering.
        n2b = graph.neighborhood(scope, alice.id, depth=2)
        assert [n.entity_id for n in n2b.nodes] == [n.entity_id for n in n2.nodes]


def test_graph_temporal_validity_filtering() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        alice = repo.add_entity(scope, "person", "Alice")
        ex = repo.add_entity(scope, "company", "ExCorp")
        current = repo.add_entity(scope, "company", "CurrentCorp")
        repo.add_relation(scope, alice.id, "works_at", ex.id, valid_to=datetime.now(UTC) - timedelta(days=30))
        repo.add_relation(scope, alice.id, "works_at", current.id)

        graph = GraphRetrievalService(repo)
        n = graph.neighborhood(scope, alice.id, depth=1)
        ids = n.entity_ids()
        assert current.id in ids
        assert ex.id not in ids  # expired relation excluded


def test_graph_related_memories_by_entity_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        project = repo.add_entity(scope, "project", "Aurora")
        team = repo.add_entity(scope, "team", "TeamX")
        repo.add_relation(scope, project.id, "staffed_by", team.id)
        _add_memory(repo, scope, "Aurora reached milestone five this quarter")
        _add_memory(repo, scope, "unrelated fact about the weather")

        graph = GraphRetrievalService(repo)
        related = graph.related_memories(scope, project.id, depth=1)
        assert len(related) == 1
        assert "Aurora" in related[0].memory.content
        assert related[0].distance == 0  # seed entity name matched


# --- multi-modal ingestion --------------------------------------------------


def test_attachment_model_validation() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope = _scope()
        ingest = IngestionService(repo)
        result = ingest.ingest(
            scope,
            "Meeting screenshot showing the dashboard",
            {"uri": "note://meeting-1"},
            attachments=[
                Attachment(
                    kind="image",
                    uri="file:///tmp/meeting-1.png",
                    media_type="image/png",
                    digest="a" * 64,
                    alt_text="Dashboard screenshot",
                    size_bytes=12345,
                ),
                Attachment(
                    kind="transcript",
                    uri="file:///tmp/meeting-1.txt",
                    alt_text="Full transcript",
                ),
            ],
        )
        observation = result.observation
        assert len(observation.attachments) == 2
        assert observation.attachments[0].kind == "image"
        assert observation.attachments[0].digest == "a" * 64
        assert observation.attachments[1].kind == "transcript"
        # Round-trips through the store.
        stored = repo.get_observation(scope, observation.id)
        assert len(stored.attachments) == 2
        assert stored.attachments[0].uri == "file:///tmp/meeting-1.png"


def test_attachment_rejects_bad_digest_and_kind() -> None:
    import pytest

    with pytest.raises(ValueError):
        Attachment(kind="image", uri="file:///x", digest="not-a-hash")
    with pytest.raises(ValueError):
        Attachment(kind="hologram", uri="file:///x")


def test_ingest_scope_bound_attachments() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, *_ = _bundle(tmp)
        scope_a = _scope("a1")
        scope_b = _scope("a2")
        ingest = IngestionService(repo)
        ingest.ingest(
            scope_a, "image description a",
            {"uri": "note://a"},
            attachments=[Attachment(kind="image", uri="file:///a.png", alt_text="A")],
        )
        ingest.ingest(scope_b, "image description b", {"uri": "note://b"})
        obs_a = repo.list_observations(scope_a)
        obs_b = repo.list_observations(scope_b)
        assert len(obs_a) == 1 and len(obs_a[0].attachments) == 1
        assert len(obs_b) == 1 and len(obs_b[0].attachments) == 0


# --- shared-scope visibility ------------------------------------------------


def test_search_shared_merges_agent_scopes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope_a = _scope("a1")
        scope_b = _scope("a2")
        _add_memory(repo, scope_a, "the search index uses sqlite fts5")
        _add_memory(repo, scope_b, "the search index uses postgres")
        proj.rebuild(scope_a)
        proj.rebuild(scope_b)
        retrieval = RetrievalService(repo, fts, vec)

        own_only = retrieval.search(scope_a, "search index", limit=10)
        assert len(own_only) == 1  # isolation preserved

        shared = retrieval.search_shared(scope_a, ["a2"], "search index", limit=10)
        contents = {r.memory.content for r in shared}
        assert "the search index uses sqlite fts5" in contents
        assert "the search index uses postgres" in contents
        assert len(shared) == 2  # merged, deduped


def test_search_shared_refuses_empty_agent_set() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, _ = _bundle(tmp)
        scope = _scope()
        retrieval = RetrievalService(repo, fts, vec)
        import pytest

        with pytest.raises(ValueError):
            retrieval.search_shared(scope, [], "anything")


def test_search_shared_does_not_leak_other_agents() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo, fts, vec, proj = _bundle(tmp)
        scope_a = _scope("a1")
        scope_b = _scope("a2")
        scope_c = _scope("a3")
        _add_memory(repo, scope_a, "alpha secret memory")
        _add_memory(repo, scope_b, "beta shared memory")
        _add_memory(repo, scope_c, "gamma private memory")
        proj.rebuild(scope_a)
        proj.rebuild(scope_b)
        proj.rebuild(scope_c)
        retrieval = RetrievalService(repo, fts, vec)
        shared = retrieval.search_shared(scope_a, ["a2"], "memory", limit=10)
        contents = {r.memory.content for r in shared}
        assert "alpha secret memory" in contents
        assert "beta shared memory" in contents
        assert "gamma private memory" not in contents  # a3 NOT shared
