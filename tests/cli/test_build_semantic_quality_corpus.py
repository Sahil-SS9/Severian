"""P0-E RED: deterministic semantic quality corpus builder contract.

The old corpus (token-permutation paraphrases, zero non-ASCII, no real
supersession relationships) is not valid model-selection evidence. The
replacement builder must prove:

- deterministic (same seed => byte-identical corpus + hash);
- >= 20% non-English queries across >= 4 languages incl >= 1 non-Latin
  script, each with a language code and provenance;
- genuine semantic paraphrases (different token sets, not permutations);
- real correction chains: old fact stale + corrected fact supersedes
  it; current-fact and historical-fact queries expect DIFFERENT
  evidence;
- real temporal before/after questions with explicit expected evidence;
- answerable fact/code/task/long probes and a separately labelled
  unanswerable/adversarial set;
- aggregate privacy checks print counts/hashes only and never text.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_semantic_quality_corpus as builder  # noqa: E402  # type: ignore[import-not-found]


def _build() -> dict[str, Any]:
    corpus = builder.build_corpus(seed=20260810)
    assert not builder.validate_corpus(corpus), builder.validate_corpus(corpus)
    return corpus


def test_corpus_is_deterministic() -> None:
    a = builder.build_corpus(seed=20260810)
    b = builder.build_corpus(seed=20260810)
    assert builder.corpus_sha256(a) == builder.corpus_sha256(b)
    assert a["observations"] == b["observations"]
    assert a["query_pairs"] == b["query_pairs"]


def test_language_coverage_contract() -> None:
    corpus = _build()
    meta = corpus["metadata"]
    assert isinstance(meta, dict)
    assert meta["non_english_query_count"] >= int(meta["total_queries"] * 0.2)
    assert len(meta["non_english_languages"]) >= 4
    assert meta["non_latin_scripts"], "at least one non-Latin script required"
    pairs = corpus["query_pairs"]
    assert isinstance(pairs, list)
    for pair in pairs:
        if pair["language"] != "en":
            assert pair.get("provenance"), "non-English query missing provenance"
    # every query carries a language code
    for pair in pairs:
        assert pair.get("language") in {"en", "fr", "de", "es", "ja", "hi"}


def test_genuine_semantic_paraphrases_not_token_permutations() -> None:
    corpus = _build()
    pairs = corpus["query_pairs"]
    assert isinstance(pairs, list)
    obs = {o["opaque_id"]: o["content"] for o in corpus["observations"]}
    checked = 0
    for pair in pairs:
        if pair["kind"] != "paraphrase":
            continue
        checked += 1
        query = pair["query"].lower()
        doc = obs[pair["evidence_ids"][0]].lower()
        # not a token permutation: stopwords/function words overlap, but
        # the content token SETS must differ meaningfully
        import re

        q_tokens = set(re.findall(r"[a-z]+", query))
        d_tokens = set(re.findall(r"[a-z]+", doc))
        shared = q_tokens & d_tokens
        assert shared, "paraphrase shares no tokens with source"
        assert len(q_tokens - d_tokens) >= 2, (
            f"paraphrase is a near-token-permutation of source: {query!r}")
    assert checked >= 5, "expected at least 5 paraphrase probes"


def test_correction_chains_have_explicit_supersession_identity() -> None:
    corpus = _build()
    obs = {o["opaque_id"]: o for o in corpus["observations"]}
    chains = [o for o in corpus["observations"] if o["supersedes"]]
    assert len(chains) >= 10
    for new_doc in chains:
        old_id = new_doc["supersedes"][0]
        old_doc = obs[old_id]
        assert old_doc["is_stale"], "superseded document must be stale"
        assert old_id != new_doc["opaque_id"]
        assert old_id in new_doc["content"] or new_doc["opaque_id"] in old_doc["content"], (
            "supersession identity must be explicit in the content")
    # current vs historical queries must expect DIFFERENT evidence
    pairs = corpus["query_pairs"]
    assert isinstance(pairs, list)
    current = [p for p in pairs if p["provenance"] == "synthetic-correction-current"]
    historical = [p for p in pairs if p["provenance"] == "synthetic-correction-historical"]
    assert current and historical
    cur_new = {p["evidence_ids"][0] for p in current}
    hist_old = {p["evidence_ids"][0] for p in historical}
    assert not (cur_new & hist_old), "a chain maps current and historical to same doc"
    assert all(obs[eid]["is_stale"] for eid in hist_old), (
        "historical queries must expect the STALE document")
    assert all(not obs[eid]["is_stale"] for eid in cur_new), (
        "current queries must expect the ACTIVE document")


def test_temporal_before_after_have_different_expected_evidence() -> None:
    corpus = _build()
    pairs = corpus["query_pairs"]
    assert isinstance(pairs, list)
    before = [p for p in pairs if p["provenance"] == "synthetic-temporal-before"]
    after = [p for p in pairs if p["provenance"] == "synthetic-temporal-after"]
    assert before and after
    before_ids = {p["evidence_ids"][0] for p in before}
    after_ids = {p["evidence_ids"][0] for p in after}
    assert not (before_ids & after_ids), "before/after must target different docs"


def test_answerable_and_unanswerable_populations() -> None:
    corpus = _build()
    pairs = corpus["query_pairs"]
    assert isinstance(pairs, list)
    kinds = {p["kind"] for p in pairs}
    for required in ("fact", "paraphrase", "correction", "temporal",
                     "code", "task", "long", "crosslang"):
        assert required in kinds, f"missing answerable kind {required}"
    for required in ("negative", "adversarial"):
        assert required in kinds, f"missing unanswerable kind {required}"
    unanswerable = [p for p in pairs if p["kind"] in ("negative", "adversarial")]
    assert unanswerable
    assert all(not p["evidence_ids"] for p in unanswerable), (
        "unanswerable probes must have NO expected evidence")


def test_privacy_scan_prints_counts_only_and_finds_nothing() -> None:
    corpus = _build()
    summary = builder.privacy_summary(corpus)
    assert summary["hits"] == 0, summary["detectors"]
    assert summary["content_sha256"] == builder.corpus_sha256(corpus)
    # the scan summary must never contain corpus text
    blob = json.dumps(summary)
    assert "Kestrel" not in blob and "thruster" not in blob


def test_manual_reviewability() -> None:
    """The corpus is committed as-is (JSON), so every query/evidence
    mapping is reviewable; the structural validator is deterministic."""
    corpus = _build()
    pairs = corpus["query_pairs"]
    assert isinstance(pairs, list)
    obs_ids = {o["opaque_id"] for o in corpus["observations"]}
    for pair in pairs:
        for eid in pair["evidence_ids"]:
            assert eid in obs_ids
        assert pair["query"].strip()
