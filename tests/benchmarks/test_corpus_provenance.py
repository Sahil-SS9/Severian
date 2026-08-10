"""P0-E residual: benchmarks-side corpus validation + provenance pins.

Validates the FROZEN round-two quality corpus
(docs/benchmarks/corpus/quality-corpus-v1.json) and its durable
provenance manifest (docs/benchmarks/manifest-quality-corpus-v1.json):

- structure: the deterministic structural validator is clean;
- taxonomy/language counts match the provenance manifest exactly;
- correction chains and temporal pairs carry explicit, DIFFERENT
  expected evidence for current vs historical and before vs after;
- the unanswerable population is evidence-free;
- privacy aggregate is zero and the test surface never emits corpus
  text — counts and hashes only (handoff P0-E).

The committed corpus file must stay byte-identical to a fresh
deterministic regeneration; that is what makes it frozen and manually
reviewable before any model lane runs.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_semantic_quality_corpus as builder  # noqa: E402

CORPUS_FILE = REPO_ROOT / "docs/benchmarks/corpus/quality-corpus-v1.json"
MANIFEST_FILE = REPO_ROOT / "docs/benchmarks/manifest-quality-corpus-v1.json"
FROZEN_CONTENT_SHA = "b836ea6016f7df44606c22d7624b325f4be1855fdd5e4dee73a7b6bfaeb5d428"
FROZEN_FILE_SHA = "83f4e4b2383f17907dec36825d4c9b86cfb593f1f19df55642d3edb027a97fb9"


def _committed() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CORPUS_FILE.read_text()))


def _provenance() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(MANIFEST_FILE.read_text()))


def test_committed_corpus_is_frozen_and_structurally_valid() -> None:
    corpus = _committed()
    errors = builder.validate_corpus(corpus)
    assert not errors, errors
    assert builder.corpus_sha256(corpus) == FROZEN_CONTENT_SHA
    assert hashlib.sha256(CORPUS_FILE.read_bytes()).hexdigest() == FROZEN_FILE_SHA
    assert corpus["metadata"]["corpus_sha256"] == FROZEN_CONTENT_SHA


def test_regeneration_is_byte_identical_to_committed_file() -> None:
    fresh = builder.build_corpus(seed=20260810)
    errors = builder.validate_corpus(fresh)
    assert not errors, errors
    assert builder.corpus_sha256(fresh) == FROZEN_CONTENT_SHA
    # mirror main(): metadata.corpus_sha256 is stamped before serialisation
    fresh["metadata"]["corpus_sha256"] = builder.corpus_sha256(fresh)
    regenerated = json.dumps(fresh, indent=2, sort_keys=True)
    assert CORPUS_FILE.read_bytes() == regenerated.encode()


def test_taxonomy_counts_match_provenance_manifest() -> None:
    meta = _committed()["metadata"]
    prov = _provenance()["taxonomy"]
    assert meta["total_documents"] == prov["total_documents"] == 1107
    assert meta["total_queries"] == prov["total_queries"] == 250
    assert meta["answerable_queries"] == prov["answerable_queries"] == 206
    assert meta["unanswerable_queries"] == prov["unanswerable_queries"] == 44
    assert meta["kind_counts"] == prov["kind_counts"]
    assert meta["language_counts"] == prov["language_counts"]
    assert meta["non_english_query_count"] == prov["non_english_query_count"] == 65
    assert meta["non_english_languages"] == prov["non_english_languages"]
    assert meta["non_latin_scripts"] == prov["non_latin_scripts"]
    assert meta["supersession_chains"] == prov["supersession_chains"] == 40
    assert meta["stale_documents"] == prov["stale_documents"] == 40


def test_language_and_script_coverage() -> None:
    meta = _committed()["metadata"]
    assert meta["non_english_query_count"] >= int(meta["total_queries"] * 0.2)
    assert len(meta["non_english_languages"]) >= 4
    assert set(meta["non_latin_scripts"]) & {"ja", "hi"}
    for pair in _committed()["query_pairs"]:
        assert pair.get("language") in {"en", "fr", "de", "es", "ja", "hi"}
        if pair["language"] != "en":
            assert pair.get("provenance"), "non-English query missing provenance"


def test_correction_chains_and_temporal_pairs_have_distinct_evidence() -> None:
    corpus = _committed()
    obs = {o["opaque_id"]: o for o in corpus["observations"]}
    chains = [o for o in corpus["observations"] if o["supersedes"]]
    assert len(chains) == 40
    for new_doc in chains:
        old_id = new_doc["supersedes"][0]
        assert old_id != new_doc["opaque_id"]
        assert obs[old_id]["is_stale"], "superseded document must be stale"
    pairs = corpus["query_pairs"]
    current = [p for p in pairs if p["provenance"] == "synthetic-correction-current"]
    historical = [p for p in pairs if p["provenance"] == "synthetic-correction-historical"]
    assert len(current) == 20 and len(historical) == 20
    cur_new = {p["evidence_ids"][0] for p in current}
    hist_old = {p["evidence_ids"][0] for p in historical}
    assert not (cur_new & hist_old), "current and historical must expect different docs"
    assert all(obs[eid]["is_stale"] for eid in hist_old)
    assert all(not obs[eid]["is_stale"] for eid in cur_new)
    before = [p for p in pairs if p["provenance"] == "synthetic-temporal-before"]
    after = [p for p in pairs if p["provenance"] == "synthetic-temporal-after"]
    assert len(before) == 20 and len(after) == 20
    assert not (
        {p["evidence_ids"][0] for p in before} & {p["evidence_ids"][0] for p in after}
    ), "before/after must target different docs"


def test_unanswerable_population_is_evidence_free() -> None:
    pairs = _committed()["query_pairs"]
    unanswerable = [p for p in pairs if p["kind"] in ("negative", "adversarial")]
    assert len(unanswerable) == 44
    assert all(not p["evidence_ids"] for p in unanswerable)


def test_privacy_aggregate_is_zero_and_text_free() -> None:
    corpus = _committed()
    summary = builder.privacy_summary(corpus)
    assert summary["hits"] == 0, summary["detectors"]
    assert summary["documents"] == 1107 and summary["queries"] == 250
    blob = json.dumps(summary)
    # counts/hashes only — the test surface must never carry corpus text
    assert "Kestrel" not in blob and "thruster" not in blob
    assert _provenance()["privacy"]["hits"] == 0
