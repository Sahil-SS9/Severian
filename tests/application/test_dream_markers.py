"""C1: maintenance markers are records about a pass, not memories of the user.

The dreaming pass stores three kinds of marker memory — ``retention_archive``, ``curation_review``
and the ``consolidated`` merge summary, which only lists ids. None of them says anything about the
user, so none of them may be returned by ``search`` or rendered by ``assemble_context``, on either
context path and not through the fill-budget neighbour read either.

The rule is deliberately NOT behind a switch: these memories should never have been searchable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from severian.application.dreaming import (
    ConsolidationService,
    CurationService,
    RetentionPolicyService,
)
from severian.application.service import SearchHitView
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Memory, Observation, Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="s2c1", collection_id="profile", agent_id="s2c1", session_id="shared"
)

# What each marker's text starts with. A search for any of these words must never return the
# marker, and the assembled context must never contain the phrase.
MARKER_PHRASES = ("Consolidated memory group:", "Curation review:", "Retention archive:")


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def say(bundle: ServiceBundle, *turns: str) -> None:
    """Save the turns as observations and derive them (the normal write path)."""
    for turn in turns:
        bundle.service.ingest_observation(SCOPE, content=turn, source={"session_id": "conv-1"})
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    assert worker.drain(gateway=bundle.gateway).failed == 0


def remember(bundle: ServiceBundle, content: str, *, kind: str = "fact") -> str:
    """One memory with its own source observation (a memory must cite a real observation)."""
    observation = bundle.repository.add_observation(
        Observation(scope=SCOPE, content=content, raw_content=content, source={"uri": f"note://{uuid.uuid4().hex}"})
    )
    memory = bundle.repository.add_memory(
        Memory(
            scope=SCOPE,
            kind=kind,
            content=content,
            source_observation_ids=[observation.id],
            model_id="test",
            prompt_version="p1",
            confidence=0.9,
        )
    )
    return memory.id


def project(bundle: ServiceBundle) -> None:
    """Apply whatever the dream services wrote to the projection (an idle drain sweeps the outbox)."""
    worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
    worker.drain(gateway=bundle.gateway)


def write_markers(bundle: ServiceBundle) -> dict[str, str]:
    """One scope holding every marker kind the dreaming pass can write, all projected.

    ``consolidate`` writes a ``consolidated`` summary; ``curate`` writes a ``curation_review``.
    Retention archive needs something old enough to archive, so it runs in its own test.
    """
    remember(bundle, "the user's bike is a blue Ridgeback tourer")
    remember(bundle, "the user's bike is a blue Ridgeback tourer")
    remember(bundle, "the user keeps the bike in the shed")
    project(bundle)
    consolidation = ConsolidationService(bundle.repository).consolidate(SCOPE)
    assert consolidation.consolidated_memory_ids, "fixture needs a consolidated summary"
    curation = CurationService(bundle.repository).curate(SCOPE)
    assert curation.review_marker_ids, "fixture needs a curation review marker"
    project(bundle)
    markers = {
        "consolidated": consolidation.consolidated_memory_ids[0],
        "curation_review": curation.review_marker_ids[0],
    }
    return markers


def test_marker_kinds_are_in_the_store_and_searchable_today(bundle: ServiceBundle) -> None:
    """The premise of the fix: the markers ARE in the store, at confidence 1.0, like a fact."""
    markers = write_markers(bundle)
    stored = {memory.id: memory for memory in bundle.repository.list_memories(SCOPE)}
    for kind, memory_id in markers.items():
        assert memory_id in stored, f"{kind} marker must exist in the store"
        assert stored[memory_id].kind == kind
        assert stored[memory_id].confidence == 1.0


@pytest.mark.parametrize("phrase", MARKER_PHRASES[:2])
def test_markers_never_appear_in_search(bundle: ServiceBundle, phrase: str) -> None:
    markers = write_markers(bundle)
    hits = bundle.service.search(SCOPE, query=phrase, limit=20)
    returned = {hit.memory_id for hit in hits}
    assert not (returned & set(markers.values())), f"{phrase!r} returned a maintenance marker"
    assert not [hit for hit in hits if hit.content.startswith(phrase)]


def test_the_real_memories_are_still_returned(bundle: ServiceBundle) -> None:
    """The filter removes the markers only: the fact the user stated is still searchable."""
    write_markers(bundle)
    hits = bundle.service.search(SCOPE, query="Ridgeback tourer", limit=20)
    assert hits, "the user's own memory must still be found"
    assert any("Ridgeback" in hit.content for hit in hits)


def test_markers_never_appear_in_context(bundle: ServiceBundle) -> None:
    write_markers(bundle)
    hits = bundle.service.search(SCOPE, query="Ridgeback tourer", limit=20)
    block = bundle.service.assemble_context(SCOPE, hits, max_chars=8000, query="Ridgeback tourer")
    for phrase in MARKER_PHRASES:
        assert phrase not in block


def test_assemble_context_drops_a_marker_handed_in_directly(bundle: ServiceBundle) -> None:
    """A caller that builds its own hit list must not be able to render a marker either."""
    markers = write_markers(bundle)
    stored = {memory.id: memory for memory in bundle.repository.list_memories(SCOPE)}
    marker = stored[markers["curation_review"]]
    handed_in = [
        SearchHitView(
            memory_id=marker.id,
            content=marker.content,
            source_observation_ids=list(marker.source_observation_ids),
            lexical_score=1.0,
            vector_score=1.0,
            fused_score=1.0,
            scope=SCOPE,
        )
    ]
    block = bundle.service.assemble_context(SCOPE, handed_in, max_chars=8000)
    assert "Curation review:" not in block


def test_markers_never_appear_in_the_v2_context(bundle: ServiceBundle, monkeypatch) -> None:
    monkeypatch.setenv("SEVERIAN_CONTEXT_FORMAT", "v2")
    markers = write_markers(bundle)
    hits = bundle.service.search(SCOPE, query="Ridgeback tourer", limit=20)
    block = bundle.service.assemble_context(SCOPE, hits, max_chars=8000, query="Ridgeback tourer")
    for phrase in MARKER_PHRASES:
        assert phrase not in block
    stored = {memory.id: memory for memory in bundle.repository.list_memories(SCOPE)}
    handed_in = [
        SearchHitView(
            memory_id=markers["consolidated"],
            content=stored[markers["consolidated"]].content,
            source_observation_ids=list(stored[markers["consolidated"]].source_observation_ids),
            lexical_score=1.0,
            vector_score=1.0,
            fused_score=1.0,
            scope=SCOPE,
        )
    ]
    assert "Consolidated memory group:" not in bundle.service.assemble_context(SCOPE, handed_in, max_chars=8000)


def test_the_fill_budget_never_appends_a_marker(bundle: ServiceBundle, monkeypatch) -> None:
    """SEVERIAN_FILL_BUDGET reads memories directly, so it has to apply the same rule."""
    monkeypatch.setenv("SEVERIAN_FILL_BUDGET", "1")
    markers = write_markers(bundle)
    hits = bundle.service.search(SCOPE, query="Ridgeback tourer", limit=1)
    assert hits
    block = bundle.service.assemble_context(SCOPE, hits, max_chars=20000, query="Ridgeback tourer")
    for phrase in MARKER_PHRASES:
        assert phrase not in block
    stored = {memory.id: memory for memory in bundle.repository.list_memories(SCOPE)}
    for memory_id in markers.values():
        assert stored[memory_id].content not in block


def test_the_retention_marker_never_appears_in_search_or_context(bundle: ServiceBundle) -> None:
    say(bundle, "the user's old flat was on Kirkstall Road", "the user's kettle is a steel one")
    service = RetentionPolicyService(bundle.repository, now=datetime.now(UTC) + timedelta(days=30))
    evaluation = service.apply_archive(SCOPE, {"max_age_days": 1, "kinds": ["fact"]})
    assert evaluation.expired_ids, "fixture needs an archived memory"
    project(bundle)
    markers = [memory for memory in bundle.repository.list_memories(SCOPE) if memory.kind == "retention_archive"]
    assert len(markers) == 1
    marker_id = markers[0].id
    hits = bundle.service.search(SCOPE, query="Retention archive memories archived by policy", limit=20)
    assert marker_id not in {hit.memory_id for hit in hits}
    block = bundle.service.assemble_context(SCOPE, hits, max_chars=8000)
    assert "Retention archive:" not in block
