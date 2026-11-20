"""The dreaming features on both backends and on the contract's in-memory fake.

Every behaviour S2 adds is backend-neutral by construction (it goes through ``CanonicalRepository``),
so each one is proved three ways: the SQLite reference backend, a live PostgreSQL backend when one is
configured (``SEVERIAN_PG_DSN``, the same lane ``test_backends_live.py`` uses), and the minimal
in-memory fake from ``test_backends.py`` that proves the contract is implementable without SQL.

C1 (markers out of ``search``/``assemble_context``) is proved here too, because the rule lives in
the retrieval and context seams rather than in a backend.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from test_backends import _FakeRepository

from severian.application.dreaming import (
    ConsolidationService,
    CurationService,
    RetentionPolicyService,
)
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import RetrievalService, SearchTuning
from severian.application.service import SearchHitView
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")


def _scope() -> Scope:
    tag = uuid.uuid4().hex[:8]
    return Scope(tenant_id=f"t-{tag}", profile_id="p1", collection_id=f"c-{tag}", agent_id="a1", session_id="s1")


@pytest.fixture(params=["sqlite", "postgres"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """A full service bundle on SQLite, or on live PostgreSQL when ``SEVERIAN_PG_DSN`` is set."""
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("SEVERIAN_PG_DSN not set; skipping live PostgreSQL lane")
        os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
        built = build_bundle(backend="postgresql", dsn=PG_DSN)
    else:
        built = build_bundle(
            database=tmp_path / "s.db",
            fts=tmp_path / "s.fts",
            vectors=tmp_path / "s.vec",
        )
    yield built
    built.close()


def provision(built: ServiceBundle) -> Scope:
    """A tenant, its collection and a scope inside it (a scope's tenant must own the collection)."""
    tag = uuid.uuid4().hex[:8]
    collection = Collection(id=f"c-{tag}", tenant_id=f"t-{tag}", name="Primary")
    built.repository.create_collection(collection)
    return Scope(tenant_id=f"t-{tag}", profile_id="p1", collection_id=collection.id, agent_id="a1", session_id="s1")


def _remember(repository: Any, scope: Scope, content: str, *, kind: str = "fact") -> Memory:
    observation = repository.add_observation(
        Observation(scope=scope, content=content, raw_content=content, source={"uri": f"note://{uuid.uuid4().hex}"})
    )
    return repository.add_memory(
        Memory(
            scope=scope,
            kind=kind,
            content=content,
            source_observation_ids=[observation.id],
            model_id="test",
            prompt_version="p1",
            confidence=0.9,
        )
    )


def _project(bundle: ServiceBundle, scope: Scope) -> None:
    """Apply pending outbox events so the projections hold what the dream services wrote."""
    events = bundle.repository.pending_outbox()
    if events:
        bundle.projection.apply(events)
        bundle.repository.ack_outbox_many([event.id for event in events])


def _markers(bundle: ServiceBundle, scope: Scope) -> dict[str, str]:
    _remember(bundle.repository, scope, "the user's bike is a blue Ridgeback tourer")
    _remember(bundle.repository, scope, "the user's bike is a blue Ridgeback tourer")
    _remember(bundle.repository, scope, "the user keeps the bike in the shed")
    _project(bundle, scope)
    consolidation = ConsolidationService(bundle.repository).consolidate(scope)
    curation = CurationService(bundle.repository).curate(scope)
    assert consolidation.consolidated_memory_ids
    assert curation.review_marker_ids
    _project(bundle, scope)
    return {
        "consolidated": consolidation.consolidated_memory_ids[0],
        "curation_review": curation.review_marker_ids[0],
    }


# -- C1: markers are not memories -------------------------------------------------------------


def test_markers_never_appear_in_search_on_either_backend(bundle: Any) -> None:
    built = bundle
    scope = provision(built)
    markers = _markers(built, scope)
    for phrase in ("Consolidated memory group", "Curation review"):
        hits = built.service.search(scope, query=phrase, limit=20)
        assert not ({hit.memory_id for hit in hits} & set(markers.values())), phrase
    assert built.service.search(scope, query="Ridgeback tourer", limit=20)


def test_markers_never_appear_in_context_on_either_backend(bundle: Any) -> None:
    built = bundle
    scope = provision(built)
    markers = _markers(built, scope)
    stored = {memory.id: memory for memory in built.repository.list_memories(scope)}
    marker = stored[markers["curation_review"]]
    handed_in = [
        SearchHitView(
            memory_id=marker.id,
            content=marker.content,
            source_observation_ids=list(marker.source_observation_ids),
            lexical_score=1.0,
            vector_score=1.0,
            fused_score=1.0,
            scope=scope,
        )
    ]
    block = built.service.assemble_context(scope, handed_in, max_chars=8000)
    assert "Curation review:" not in block


def test_the_retention_marker_is_hidden_on_either_backend(bundle: Any) -> None:
    from datetime import UTC, datetime, timedelta

    built = bundle
    scope = provision(built)
    _remember(built.repository, scope, "the user's old flat was on Kirkstall Road")
    _project(built, scope)
    service = RetentionPolicyService(built.repository, now=datetime.now(UTC) + timedelta(days=30))
    applied = service.apply_archive(scope, {"max_age_days": 1, "kinds": ["fact"]})
    assert applied.expired_ids
    _project(built, scope)
    markers = [memory for memory in built.repository.list_memories(scope) if memory.kind == "retention_archive"]
    assert len(markers) == 1
    hits = built.service.search(scope, query="Retention archive memories archived by policy", limit=20)
    assert markers[0].id not in {hit.memory_id for hit in hits}


# -- C1 on the contract fake: the rule is not SQL-specific ------------------------------------


def test_the_fake_backend_cannot_return_a_marker_from_search(tmp_path: Path) -> None:
    """The retrieval seam applies the rule with no SQL in sight (the fake has none)."""
    repository = _FakeRepository()
    scope = _scope()
    marker = _remember(repository, scope, "Curation review: 12 memories scored", kind="curation_review")
    real = _remember(repository, scope, "the user keeps a steel kettle")
    lexical = FTS5Index(tmp_path / "f.fts")
    vector = LocalVectorIndex(tmp_path / "f.vec", embedding=HashEmbedding())
    projection = ProjectionCoordinator(repository=repository, lexical=lexical, vector=vector)
    projection.index_memory(marker)
    projection.index_memory(real)
    retrieval = RetrievalService(repository, lexical, vector, SearchTuning())

    hits = retrieval.search(scope, "Curation review memories scored", limit=20)
    assert marker.id not in {hit.memory.id for hit in hits}
    assert real.id in {hit.memory.id for hit in hits}
    assert retrieval.search(scope, "*", limit=20) and marker.id not in {
        hit.memory.id for hit in retrieval.search(scope, "*", limit=20)
    }
