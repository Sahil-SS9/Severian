"""Regression tests for benchmark-driven retrieval fixes.

These cover the defects the provider benchmark surfaced on its first run:
stopword pollution in the hash embedding, the missing relevance floor,
FTS AND semantics hiding real lexical matches, and observation-level
supersession so corrected facts never resurface.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from severian.benchmarks.adapters.severian import SeverianAdapter
from severian.domain.models import Scope
from severian.infrastructure.vector import HashEmbedding


def _corpus_docs() -> list[dict[str, object]]:
    manifest = json.loads(Path("docs/benchmarks/manifest-v1.json").read_text())
    return manifest["corpus"]["documents"]  # type: ignore[no-any-return]


def _queries() -> list[dict[str, object]]:
    manifest = json.loads(Path("docs/benchmarks/manifest-v1.json").read_text())
    return manifest["queries"]  # type: ignore[no-any-return]


def _expected_evidence_ids(query: dict[str, object]) -> list[str]:
    raw = query.get("expected_evidence", [])
    if not isinstance(raw, list):
        return []
    return [str(evidence["document_id"]) for evidence in raw if isinstance(evidence, dict)]


@pytest.fixture()
def bench() -> tuple[SeverianAdapter, Scope, Scope]:
    adapter = SeverianAdapter(provider_id="test", backend="sqlite")
    for doc in _corpus_docs():
        supersedes_raw = doc.get("supersedes", [])
        supersedes = [str(x) for x in supersedes_raw] if isinstance(supersedes_raw, list) else []
        adapter.ingest(
            str(doc["id"]),
            str(doc["content"]),
            scope=str(doc["scope"]),
            supersedes=supersedes,
        )
    t1 = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    t2 = Scope(tenant_id="t2", profile_id="p2", collection_id="c2", agent_id="a2", session_id="s2")
    return adapter, t1, t2


def test_stopwords_no_longer_pollute_embeddings() -> None:
    # Stopword-only queries must produce a zero vector, not a spurious
    # positive dot product against every document.
    embedding = HashEmbedding(dimension=256)
    query_vector = embedding.encode("Who walked on the surface of Mars in 1972?")
    doc_vector = embedding.encode("The team migrated the core service from Python to Rust in 2025.")
    dot = sum(a * b for a, b in zip(query_vector, doc_vector, strict=True))
    assert dot == pytest.approx(0.0, abs=1e-9)


def test_fts_uses_or_semantics_for_natural_language() -> None:
    # AND semantics (FTS5 default) requires every token to match, so a
    # natural-language query with filler words returns nothing even when
    # the document contains the distinctive terms. OR + BM25 surfaces
    # distinctive matches first.
    from severian.infrastructure.fts import FTS5Index

    assert " OR " in FTS5Index._match_query("How does the system keep old facts from reappearing?")


def test_fts_filters_stopwords_from_match_query() -> None:
    from severian.infrastructure.fts import FTS5Index

    match = FTS5Index._match_query("What is the office address of the team?")
    # "what", "is", "the", "of" are stopwords; distinctive terms remain.
    assert "office" in match and "address" in match
    assert "what" not in match


def test_negative_query_returns_nothing_with_relevance_floor(bench: tuple[SeverianAdapter, Scope, Scope]) -> None:
    adapter, t1, _ = bench
    hits = adapter._bundle.service.search(
        t1, query="Who walked on the surface of Mars in 1972?", limit=10, derive=True,
        gateway=adapter._bundle.gateway, min_vector_score=0.15,
    )
    assert hits == []


def test_all_answerable_queries_rank_expected_evidence_first(bench: tuple[SeverianAdapter, Scope, Scope]) -> None:
    adapter, t1, t2 = bench
    scopes = {"t1/p1/c1/a1/s1": t1, "t2/p2/c2/a2/s2": t2}
    failures: list[str] = []
    for query in _queries():
        expected = _expected_evidence_ids(query)
        if not expected:
            continue
        scope = scopes.get(str(query["scope"]))
        assert scope is not None, f"unknown scope for {query['id']}"
        hits = adapter._bundle.service.search(
            scope, query=str(query["query"]), limit=10, derive=True,
            gateway=adapter._bundle.gateway, min_vector_score=0.15,
        )
        ranked: list[str] = []
        for hit in hits:
            for obs in hit.source_observation_ids:
                if obs in adapter._obs_to_doc:
                    doc = adapter._obs_to_doc[obs]
                    if doc not in ranked:
                        ranked.append(doc)
        if expected[0] not in ranked or ranked.index(expected[0]) != 0:
            failures.append(f"{query['id']}: expected {expected[0]} first, got {ranked[:3]}")
    assert failures == [], "\n".join(failures)


def test_superseded_observation_never_resurfaces(bench: tuple[SeverianAdapter, Scope, Scope]) -> None:
    adapter, _, t2 = bench
    # d24 (old address) is superseded by d25; asking for the old address
    # must surface the current head, never the tombstoned fact.
    hits = adapter._bundle.service.search(
        t2, query="What was the old office address?", limit=10, derive=True,
        gateway=adapter._bundle.gateway, min_vector_score=0.15,
    )
    ranked: list[str] = []
    for hit in hits:
        for obs in hit.source_observation_ids:
            if obs in adapter._obs_to_doc:
                doc = adapter._obs_to_doc[obs]
                if doc not in ranked:
                    ranked.append(doc)
    assert "d24" not in ranked
    assert ranked and ranked[0] == "d25"


def test_cross_scope_isolation_in_benchmark_corpus(bench: tuple[SeverianAdapter, Scope, Scope]) -> None:
    adapter, t1, _ = bench
    hits = adapter._bundle.service.search(
        t1, query="What is the private launch date of Project Chrysalis?", limit=10, derive=True,
        gateway=adapter._bundle.gateway, min_vector_score=0.15,
    )
    ranked: list[str] = []
    for hit in hits:
        for obs in hit.source_observation_ids:
            if obs in adapter._obs_to_doc:
                doc = adapter._obs_to_doc[obs]
                if doc not in ranked:
                    ranked.append(doc)
    # d21 lives in t2; t1 must never surface it.
    assert "d21" not in ranked
    assert ranked and ranked[0] == "d26"


def test_abstention_floor_returns_nothing_for_unanswerable() -> None:
    """LOCOMO q11 class: pure hash-noise probes must abstain.

    "What is the company's annual revenue?" has no lexical match in the
    corpus, but hash embeddings collide with unrelated docs producing
    spurious vector hits. The fused-score floor rejects them (fused
    ~0.016 < 0.02) — the honest empty result. Genuine queries (lexical
    match present) are unaffected.
    """
    adapter = SeverianAdapter(provider_id="test", backend="sqlite", min_fused_score=0.02)
    manifest = json.loads(Path("docs/benchmarks/manifest-locomo-tier-v1.json").read_text())
    for doc in manifest["corpus"]["documents"]:
        adapter.ingest(str(doc["id"]), str(doc["content"]), scope=str(doc["scope"]))

    # Unanswerable noise probe -> abstain.
    resp = adapter.query(
        "What is the company's annual revenue?",
        limit=10,
        scope="t1/p1/c1/a1/s1",
        query_kind="negative",
    )
    assert resp.items == [], f"noise probe must abstain, got {resp.items[:3]}"

    # Genuine probe still retrieves its target first.
    resp2 = adapter.query(
        "What will Ada implement next week?",
        limit=10,
        scope="t1/p1/c1/a1/s1",
        query_kind="fact",
    )
    assert resp2.items and resp2.items[0]["document_id"] == "c01"
