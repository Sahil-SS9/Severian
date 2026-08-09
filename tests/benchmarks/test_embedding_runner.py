"""Contract tests for the embedding bake-off runner (zero-debt plan 1.2)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from severian.benchmarks.adapters.severian import SeverianAdapter  # noqa: E402
from severian.benchmarks.embedding_runner import (  # noqa: E402
    EmbeddingBakeoffRunner,
    enumerate_providers,
)
from severian.benchmarks.manifest import BenchmarkManifest  # noqa: E402
from severian.infrastructure.vector import HashEmbedding  # noqa: E402


def _manifest(tmp_path: Path, n_docs: int = 30, n_queries: int = 8) -> BenchmarkManifest:
    payload = {
        "schema_version": 1,
        "manifest_id": "embedding-contract",
        "name": "contract",
        "corpus": {
            "id": "c1", "version": "1", "seed": 1,
            "documents": [
                {"id": f"d{i}", "content": f"fact {i}: the service handles {i*7} requests per second",
                 "scope": "t1/p1/c1/a1/s1"}
                for i in range(n_docs)
            ],
        },
        "queries": [
            {"id": f"q{i}", "kind": "fact", "query": f"service handles {i*7} requests",
             "scope": "t1/p1/c1/a1/s1",
             "answerable": True,
             "expected_evidence": [{"document_id": f"d{i}", "weight": 1.0}]}
            for i in range(n_queries)
        ]
        + [
            {"id": "q-temporal", "kind": "temporal", "query": "old fact",
             "scope": "t1/p1/c1/a1/s1", "answerable": True,
             "expected_evidence": [{"document_id": "d0", "weight": 1.0}]},
            {"id": "q-negative", "kind": "negative", "query": "unrelated",
             "scope": "t1/p1/c1/a1/s1", "answerable": False, "expected_evidence": []},
            {"id": "q-adversarial", "kind": "adversarial", "query": "private marker",
             "scope": "t1/p1/c1/a1/s1", "answerable": False, "expected_evidence": []},
        ],
        "providers": [
            {"id": "hash", "kind": "severian", "deployment": "local",
             "backend": "sqlite",
             "model": {"id": "hash", "prompt_version": "v1"},
             "config": {"dimension": 256, "normalize": True, "runtime": "pure-python",
                        "quantisation": "none", "revision": "n/a", "model_dir": None}},
            {"id": "jina-v4", "kind": "severian", "deployment": "local",
             "backend": "sqlite",
             "model": {"id": "jinaai/jina-embeddings-v4", "prompt_version": "n/a"},
             "vendor_reported_only": True,
             "config": {"reference_url": "https://huggingface.co/jinaai/jina-embeddings-v4",
                        "exclusion_reason": "excluded"}},
        ],
        "metrics": ["recall_at_10", "mrr", "ndcg_at_10", "citation_precision",
                    "stale_fact_rate", "temporal_accuracy", "cross_scope_leakage",
                    "unsupported_answer_rate", "latency_p95_ms", "cost_per_query_usd"],
        "release_gate": {"min_recall_at_10": 0.0, "min_citation_precision": 0.0,
                         "max_stale_fact_rate": 1.0, "max_cross_scope_leakage": 1.0,
                         "max_unsupported_answer_rate": 1.0,
                         "require_external_providers": 0},
    }
    return BenchmarkManifest.model_validate(payload)


def _hash_factory(workdir: Path) -> object:
    return SeverianAdapter(
        provider_id="hash", backend="sqlite", dimension=256,
        embedding=HashEmbedding(dimension=256), reset_db=False,
        workdir=str(workdir),
    )


def test_manifest_with_new_query_kinds_validates() -> None:
    payload = {
        "schema_version": 1, "manifest_id": "kinds", "name": "kinds",
        "corpus": {"id": "c", "version": "1", "seed": 1,
                   "documents": [{"id": "d0", "content": "x", "scope": "s"}]},
        "queries": [
            {"id": f"q{i}", "kind": kind, "query": "x", "scope": "s",
             "answerable": kind not in {"negative", "adversarial"},
             "expected_evidence": ([{"document_id": "d0", "weight": 1.0}]
                                   if kind not in {"negative", "adversarial"} else [])}
            for i, kind in enumerate(["code", "crosslang", "long", "temporal", "negative", "adversarial"])
        ],
        "providers": [{"id": "hash", "kind": "severian", "deployment": "local",
                       "backend": "sqlite", "model": {"id": "hash", "prompt_version": "v1"},
                       "config": {}}],
        "metrics": ["recall_at_10", "mrr", "ndcg_at_10", "citation_precision",
                    "stale_fact_rate", "temporal_accuracy", "cross_scope_leakage",
                    "unsupported_answer_rate", "latency_p95_ms", "cost_per_query_usd"],
        "release_gate": {"require_external_providers": 0},
    }
    manifest = BenchmarkManifest.model_validate(payload)
    assert len(manifest.queries) == 6


def test_enumerate_providers_excludes_vendor_reported() -> None:
    manifest = _manifest(Path("/tmp"))
    ids = [pid for pid, _ in enumerate_providers(manifest)]
    assert "hash" in ids
    assert "jina-v4" not in ids, "excluded (vendor_reported) lane must not run"


def test_hash_lane_runs_and_records_model_tier(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    runner = EmbeddingBakeoffRunner(
        manifest=manifest,
        lane_factory={"hash": lambda: _hash_factory(tmp_path)},
    )
    result = runner.run()
    assert len(result.lanes) == 1
    lane = result.lanes[0]
    assert lane.skipped is False, lane.skip_reason
    assert lane.provider_id == "hash"
    assert lane.model_id == "hash"
    assert lane.dimension == 256
    assert lane.quality.queries_run == 11
    assert lane.query_latency_p95_ms >= 0.0
    assert lane.cold_load_seconds >= 0.0
    assert lane.vector_bytes_per_memory == 1024  # 256 * 4
    assert lane.peak_rss_mb >= 0.0
    assert lane.encode_throughput_docs_per_sec > 0.0
    assert lane.index_build_seconds >= 0.0
    assert lane.artifact_bytes == 0  # hash has no model artefact


def test_missing_lane_factory_is_recorded_not_dropped(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    runner = EmbeddingBakeoffRunner(manifest=manifest, lane_factory={})
    result = runner.run()
    assert len(result.lanes) == 1
    lane = result.lanes[0]
    assert lane.skipped is True
    assert "no lane factory" in lane.skip_reason


def test_inject_corpus_merges_observations_and_pairs(tmp_path: Path) -> None:
    from run_embedding_benchmark import _inject_corpus

    manifest = _manifest(tmp_path, n_docs=1, n_queries=1)
    corpus_data = {
        "observations": [
            {"scope": "t1/p1/c1/a1/s1", "opaque_id": "oid-1",
             "content": "the backup job runs at 02:00 utc"},
            {"scope": "t1/p1/c1/a1/s2", "opaque_id": "oid-2",
             "content": "the queue depth reached 42 messages"},
        ],
        "query_pairs": [
            {"query": "backup job time", "evidence": ["the backup job runs at 02:00 utc"],
             "kind": "fact"},
            {"query": "unrelated topic 0", "evidence": [], "kind": "negative"},
            {"query": "old backup schedule", "evidence": ["the backup job runs at 02:00 utc"],
             "kind": "temporal"},
            {"query": "private marker", "evidence": [], "kind": "adversarial"},
        ],
    }
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps(corpus_data))

    merged = _inject_corpus(manifest, corpus_path)
    assert len(merged.corpus.documents) == 2
    assert len(merged.queries) == 4
    assert merged.queries[0].answerable is True
    assert merged.queries[0].expected_evidence[0].document_id == "oid-1"
    assert merged.queries[0].scope == "t1/p1/c1/a1/s1"
    assert merged.queries[1].answerable is False
    assert merged.queries[1].expected_evidence == []


def test_result_to_dict_is_jsonable(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    runner = EmbeddingBakeoffRunner(
        manifest=manifest,
        lane_factory={"hash": lambda: _hash_factory(tmp_path)},
    )
    result = runner.run()
    payload = result.to_dict()
    json.dumps(payload)  # must not raise
    assert payload["lanes"][0]["quality"]["queries_run"] == 11
    assert payload["corpus_sha256"] == ""
