"""Tests for deterministic entity/relation extraction in the derive path (P1c).

Covers:
- proper-noun entity extraction (kinds, dedupe, stopword rejection)
- typed relation extraction from templates
- derive-time wiring: derive_observation populates the Entity/Relation
  store through EntityExtractionService
- determinism: same content -> same entities/relations
- scope-bound: extraction never crosses the 5-D boundary
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from severian.application.derivation import DerivationService
from severian.application.extraction import (
    EntityExtractionService,
    extract_entities,
    extract_relations,
)
from severian.application.ingestion import IngestionService
from severian.domain.models import Collection, Scope
from severian.infrastructure.sqlite import SQLiteRepository


class _HashingGateway:
    model_id = "local/hashing"
    prompt_version = "prompt-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]


def _bundle(tmp: str):
    root = Path(tmp)
    repo = SQLiteRepository(path=root / "sev.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    return repo


def _scope(agent: str = "a1") -> Scope:
    return Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1", agent_id=agent, session_id="s1"
    )


# --- pure extraction functions ----------------------------------------------


def test_extract_entities_proper_nouns_with_kinds() -> None:
    content = "Alice Johnson works at Acme Corp. She leads the Aurora Project team."
    entities = extract_entities(content)
    by_name = {name.casefold(): (kind, name) for kind, name in entities}
    # Acme Corp -> org (suffix), Aurora Project -> project (cue)
    assert by_name["acme corp"][0] == "org"
    assert by_name["aurora project"][0] == "project"
    # Alice Johnson detected as proper noun; "She" is not capitalised in
    # this content, so no person classification from title cues.
    assert "alice johnson" in by_name


def test_extract_entities_rejects_stopwords_and_dedupes() -> None:
    content = "The New Project uses the New Project framework. Old data was migrated."
    entities = extract_entities(content)
    names = [name for _kind, name in entities]
    # "The" and "New" alone rejected; "New Project" deduped to one.
    assert names.count("New Project") == 1
    assert "The" not in names
    # "Old" alone is a stopword; but "Old data" is a 2-word phrase — the
    # leading stopword is trimmed, leaving "data" (a lowercase word that
    # fails the capitalised-phrase match), so no entity named "Old".
    assert all(name != "Old" for name in names)


def test_extract_relations_typed_templates() -> None:
    content = "Alice Johnson works at Acme Corp."
    entities = extract_entities(content)
    relations = extract_relations(content, entities)
    assert any(
        subj == "Alice Johnson" and pred == "works_at" and obj == "Acme Corp"
        for subj, pred, obj in relations
    )


def test_extract_relations_no_self_relation() -> None:
    content = "Acme Corp is located in London."
    entities = extract_entities(content)
    relations = extract_relations(content, entities)
    for subj, _pred, obj in relations:
        assert subj.casefold() != obj.casefold()


def test_extraction_is_deterministic() -> None:
    content = "Project Aurora is staffed by Team X at the London office."
    first_entities = extract_entities(content)
    first_relations = extract_relations(content, first_entities)
    for _ in range(3):
        assert extract_entities(content) == first_entities
        assert extract_relations(content, first_entities) == first_relations


# --- derive-path wiring ------------------------------------------------------


def test_derive_populates_entities_and_relations() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _bundle(tmp)
        scope = _scope()
        extraction = EntityExtractionService(repo)
        derive = DerivationService(repo, extraction=extraction)
        ingest = IngestionService(repo)

        result = ingest.ingest(
            scope, "Alice Johnson works at Acme Corp.", {"uri": "note://1"}
        )
        derive.derive_observation(scope, result.observation.id, _HashingGateway())

        entities = repo.list_entities(scope)
        by_name = {e.name.casefold(): e for e in entities}
        assert "alice johnson" in by_name
        assert "acme corp" in by_name
        relations = repo.list_relations(scope)
        assert len(relations) == 1
        assert relations[0].predicate == "works_at"


def test_derive_is_idempotent_on_entities() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _bundle(tmp)
        scope = _scope()
        extraction = EntityExtractionService(repo)
        derive = DerivationService(repo, extraction=extraction)
        ingest = IngestionService(repo)

        result = ingest.ingest(
            scope, "Alice Johnson works at Acme Corp.", {"uri": "note://1"}
        )
        derive.derive_observation(scope, result.observation.id, _HashingGateway())
        derive.derive_observation(scope, result.observation.id, _HashingGateway())

        entities = repo.list_entities(scope)
        assert len(entities) == 2  # Alice Johnson + Acme Corp, no dupes
        relations = repo.list_relations(scope)
        assert len(relations) == 1  # idempotent


def test_derive_skips_superseded_observation_extraction() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _bundle(tmp)
        scope = _scope()
        extraction = EntityExtractionService(repo)
        derive = DerivationService(repo, extraction=extraction)
        ingest = IngestionService(repo)

        result = ingest.ingest(
            scope, "Alice Johnson works at Acme Corp.", {"uri": "note://1"}
        )
        derive.derive_observation(scope, result.observation.id, _HashingGateway())
        # revise through the SERVICE path -> observation superseded AND
        # memories tombstoned (P0-5)
        repo.revise_observation(scope, result.observation.id, "Alice Johnson works at Beta Ltd.")
        # derive the old (now-superseded) observation again: guard returns
        # [] — no NEW memories, and extraction is skipped.
        derived = derive.derive_observation(scope, result.observation.id, _HashingGateway())
        assert derived == []
        # no new entities from the superseded obs (Beta Ltd. must not appear)
        entity_names = [e.name for e in repo.list_entities(scope)]
        assert "Beta Ltd." not in entity_names


def test_extraction_is_scope_bound() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _bundle(tmp)
        scope_a = _scope("a1")
        scope_b = _scope("a2")
        extraction = EntityExtractionService(repo)
        derive = DerivationService(repo, extraction=extraction)
        ingest = IngestionService(repo)

        result = ingest.ingest(
            scope_a, "Alice Johnson works at Acme Corp.", {"uri": "note://1"}
        )
        derive.derive_observation(scope_a, result.observation.id, _HashingGateway())

        # scope b sees nothing
        assert repo.list_entities(scope_b) == []
        assert repo.list_relations(scope_b) == []
        # scope a has both
        assert len(repo.list_entities(scope_a)) == 2
