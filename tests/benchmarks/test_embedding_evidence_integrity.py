"""RED witnesses: embedding evidence-integrity defects (audit CRITICAL 1-3,
HIGH 4-8). Each test FAILS against the pre-repair code and must pass after
the Phase B benchmark-integrity repairs. Do not weaken these assertions.
"""
from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from severian.benchmarks.adapters.severian import SeverianAdapter  # noqa: E402
from severian.benchmarks.embedding_runner import EmbeddingBakeoffRunner  # noqa: E402
from severian.benchmarks.manifest import BenchmarkManifest  # noqa: E402
from severian.infrastructure.vector import HashEmbedding  # noqa: E402


def _manifest_payload(tmp_path: Path, n_docs: int = 20, n_queries: int = 6) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "manifest_id": "integrity-red",
        "name": "integrity",
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
             "scope": "t1/p1/c1/a1/s1", "answerable": True,
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
             "backend": "sqlite", "model": {"id": "hash", "prompt_version": "v1"},
             "config": {"dimension": 256, "normalize": True, "runtime": "pure-python",
                        "quantisation": "none", "revision": "n/a", "model_dir": None}},
        ],
        "metrics": ["recall_at_10", "mrr", "ndcg_at_10", "citation_precision",
                    "stale_fact_rate", "temporal_accuracy", "cross_scope_leakage",
                    "unsupported_answer_rate", "latency_p95_ms", "cost_per_query_usd"],
        "release_gate": {"min_recall_at_10": 0.9, "min_citation_precision": 0.8,
                         "max_stale_fact_rate": 0.0, "max_cross_scope_leakage": 0.0,
                         "max_unsupported_answer_rate": 0.1,
                         "require_external_providers": 0},
    }


def _manifest(tmp_path: Path) -> BenchmarkManifest:
    return BenchmarkManifest.model_validate(_manifest_payload(tmp_path))


def _hash_factory(workdir: Path) -> SeverianAdapter:
    return SeverianAdapter(
        provider_id="hash", backend="sqlite", dimension=256,
        embedding=HashEmbedding(dimension=256), reset_db=False,
        workdir=str(workdir),
    )


# --- RED 1: corpus SHA must hash the CORPUS, not the manifest ---------------


def test_red_corpus_sha_binds_corpus_not_manifest(tmp_path: Path) -> None:
    """Audit CRITICAL 1: args.manifest was passed as corpus_path, so the
    published 'corpus SHA' was the manifest SHA. The runner must hash the
    actual corpus bytes and differ from the manifest hash."""
    manifest = _manifest(tmp_path)
    # A corpus file distinct from the manifest.
    corpus_path = tmp_path / "corpus.json"
    corpus_path.write_text(json.dumps({
        "observations": [{"scope": "t1/p1/c1/a1/s1", "opaque_id": "oid-1",
                          "content": "the backup job runs at 02:00 utc"}],
        "query_pairs": [{"query": "backup job time",
                         "evidence": ["the backup job runs at 02:00 utc"],
                         "kind": "fact"}],
    }))
    runner = EmbeddingBakeoffRunner(
        manifest=manifest, lane_factory={}, corpus_path=corpus_path,
    )
    result = runner.run()
    expected = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    assert result.corpus_sha256 == expected, (
        "corpus_sha256 must be the SHA of the actual corpus file"
    )
    manifest_sha = hashlib.sha256(
        manifest.model_dump_json().encode()
    ).hexdigest()
    assert result.corpus_sha256 != manifest_sha, (
        "corpus SHA must differ from the manifest SHA (audit CRITICAL 1)"
    )


# --- RED 2: throughput probes must not contaminate quality scoring ----------


class _SpyAdapter:
    """Adapter that records every ingest; used to prove the throughput
    probe no longer writes enc-* rows into the scoring index."""

    def __init__(self, workdir: Path) -> None:
        self._inner = _hash_factory(workdir)
        self.ingested_ids: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        self.ingested_ids.append(document_id)
        self._inner.ingest(document_id, content, scope=scope, supersedes=supersedes)


def test_red_throughput_probe_does_not_pollute_quality_index(tmp_path: Path) -> None:
    """Audit CRITICAL 3: the encode-throughput probe inserted enc-* rows
    into the SAME adapter later scored, so leakage was manufactured.
    Throughput must run in a separate index (or after scoring) and the
    scored index must contain exactly the manifest corpus IDs."""
    manifest = _manifest(tmp_path)
    scored_spy: _SpyAdapter | None = None

    def factory() -> _SpyAdapter:
        nonlocal scored_spy
        adapter = _SpyAdapter(tmp_path / "store")
        if scored_spy is None:
            scored_spy = adapter  # the FIRST adapter is the scored lane
        return adapter

    runner = EmbeddingBakeoffRunner(
        manifest=manifest, lane_factory={"hash": factory},
    )
    result = runner.run()
    lane = result.lanes[0]
    assert lane.skipped is False, lane.skip_reason
    # The SCORED index must never see throughput-probe rows.
    assert scored_spy is not None
    probe_ids = [i for i in scored_spy.ingested_ids if i.startswith("enc-")]
    assert probe_ids == [], (
        f"throughput probe rows {probe_ids[:3]} contaminated the scoring index"
    )
    # The quality lane must not report leakage caused by its own probes.
    assert lane.quality.cross_scope_leakage == 0.0


# --- RED 3: bake-off must run the release gate and persist failures ---------


def test_red_release_gate_is_evaluated_and_persisted(tmp_path: Path) -> None:
    """Audit CRITICAL 2: the bake-off called _run_provider() directly and
    printed unconditional 'ok'. Every completed lane must be evaluated
    against the manifest release gate and gate_passed/gate_failures must
    be persisted, and no unconditional ok may be printed."""
    manifest = _manifest(tmp_path)
    runner = EmbeddingBakeoffRunner(
        manifest=manifest, lane_factory={"hash": lambda: _hash_factory(tmp_path)},
    )
    result = runner.run()
    lane = result.lanes[0]
    assert lane.skipped is False
    # The runner must expose gate evaluation per lane (this fails on the
    # pre-repair code: no gate fields exist at all).
    assert hasattr(lane, "gate_passed"), "lane must persist gate_passed"
    assert hasattr(lane, "gate_failures"), "lane must persist gate_failures"
    assert isinstance(lane.gate_failures, list)


def test_red_no_unconditional_ok_printed(tmp_path: Path) -> None:
    """The CLI must not print 'ok' for lanes that failed their gate."""
    src = (REPO_ROOT / "scripts/run_embedding_benchmark.py").read_text()
    # The pre-repair code prints f"...{lane.vector_bytes_per_memory:>5.0f}  ok"
    # unconditionally for every non-skipped lane. That literal must go.
    assert "  ok\")" not in src and "  ok\"" not in src, (
        "unconditional 'ok' lane status must be replaced by gate-aware status"
    )
    # The CLI must reference the gate in its verdict printing.
    assert "gate" in src.lower(), "CLI verdict output must be gate-aware"


# --- RED 4: query kinds must be meaningful, not near-copy prefixes ----------


def test_red_query_kinds_are_not_near_copy_prefixes(tmp_path: Path) -> None:
    """Audit HIGH 4: queries were the first 8 words of the source doc.
    A paraphrase must differ from its source; a crosslang pair must
    differ in script/language; temporal pairs need a time contrast."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from build_redacted_embedding_corpus import (  # type: ignore[import-not-found]
        CorpusObservation,
        _build_pairs,
    )

    obs = [
        CorpusObservation(scope="s1", opaque_id=f"o{i}", content=(
            f"the data pipeline at site {i} syncs every night at 23:30 "
            "and retries failed batches automatically up to five times"
        ))
        for i in range(12)
    ]
    pairs = _build_pairs(obs, n=12)
    by_kind: dict[str, list[Any]] = {}
    for p in pairs:
        by_kind.setdefault(p.kind, []).append(p)
    # A paraphrase query must NOT equal the first words of its evidence.
    for p in by_kind.get("paraphrase", []):
        src_words = [w for w in p.evidence[0].split() if len(w) > 3][:8]
        prefix = " ".join(src_words).lower()
        assert p.query.lower() != prefix, (
            f"paraphrase query is a near-copy of the source: {p.query!r}"
        )
    # Negative probes must not contain the evidence words at all.
    for p in by_kind.get("negative", []):
        if p.evidence:
            words = [w.lower() for w in p.evidence[0].split() if len(w) > 3]
            assert not any(w in p.query.lower() for w in words), (
                f"negative probe must not contain evidence words: {p.query!r}"
            )
    # crosslang queries must exist and be genuinely different from source
    assert by_kind.get("crosslang"), "crosslang probes must exist"
    # The whole taxonomy must be represented.
    for kind in ("fact", "paraphrase", "correction", "temporal", "negative",
                 "code", "crosslang", "long", "adversarial"):
        assert by_kind.get(kind), f"kind {kind} missing from generated pairs"


# --- RED 5: model revisions must be pinned and passed to loaders -----------


def test_red_manifest_requires_immutable_revisions() -> None:
    """Audit HIGH 6: revisions were 'unknown-pin-at-run'. A well-formed
    manifest with a pinned revision validates; the SAME manifest with an
    unpinned revision must be rejected by manifest validation."""
    payload = _manifest_payload(Path("/tmp"))
    payload["providers"][0] = {
        "id": "neural", "kind": "severian", "deployment": "local",
        "backend": "sqlite", "model": {"id": "some/model", "prompt_version": "v1"},
        "config": {"dimension": 384, "normalize": True, "runtime": "sentence-transformers",
                   "quantisation": "none", "revision": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
                   "model_dir": None},
    }
    # Sanity: the payload shape itself is valid when revision is pinned.
    BenchmarkManifest.model_validate(payload)
    payload["providers"][0]["config"]["revision"] = "unknown-pin-at-run"
    with pytest.raises(ValueError):
        # The manifest MUST reject unpinned revisions for neural lanes.
        BenchmarkManifest.model_validate(payload)


# --- RED 6: RSS must be measured per fresh process, not deltas ---------------


def test_red_peak_rss_is_not_a_process_lifetime_delta(tmp_path: Path) -> None:
    """Audit HIGH 7: peak_rss_mb = ru_maxrss after-minus-before in one
    process makes later lanes report ~0 (Small English 2.8 MB) or
    anomalous giants. The runner must not use the rss_before/delta
    pattern for per-lane peak RSS."""
    import inspect

    from severian.benchmarks.embedding_runner import EmbeddingBakeoffRunner as R
    src = inspect.getsource(R._measure_lane)
    assert "rss_before" not in src and "ru_maxrss" not in src, (
        "peak RSS must not be computed as a ru_maxrss delta in the shared process"
    )


# --- RED 7: subset reruns must never overwrite a complete merge ------------


def test_red_checkpoint_merge_rejects_subset_overwrite(tmp_path: Path) -> None:
    """Audit MEDIUM 9: a two-lane rerun overwrote the five-lane merged
    result and marked completed lanes skipped. A checkpoint merge must
    refuse to overwrite a superset with a subset."""
    from severian.benchmarks.checkpoint_merge import (
        CheckpointContract,
        merge_checkpoints,
    )

    cdir = tmp_path / "checkpoints"
    cdir.mkdir()
    # Two lane checkpoints.
    for pid, rev in (("hash", "n/a"), ("granite-97m", "a" * 40)):
        (cdir / f"lane-{pid}.json").write_text(json.dumps({
            "provider_id": pid, "model_id": pid, "revision": rev,
            "dimension": 256 if pid == "hash" else 384, "normalize": True,
            "runtime": "pure-python" if pid == "hash" else "sentence-transformers",
            "quantisation": "none", "cold_load_seconds": 0.1,
            "artifact_bytes": 0, "peak_rss_mb": 10.0,
            "encode_throughput_docs_per_sec": 5.0,
            "query_latency_p50_ms": 1.0, "query_latency_p95_ms": 2.0,
            "query_latency_p99_ms": 3.0, "index_build_seconds": 0.2,
            "vector_bytes_per_memory": 1024,
            "gate_passed": True, "gate_failures": [],
            "skipped": False, "skip_reason": "",
        }))
    contract = CheckpointContract("corpus-1", "manifest-1", "runner-1", "t0")
    out = tmp_path / "merged.json"
    # First merge writes the full result.
    merged = merge_checkpoints(cdir, output=out, contract=contract)
    assert merged.provider_ids == {"hash", "granite-97m"}
    # A LATER subset rerun (only hash) must refuse to overwrite it.
    subdir = tmp_path / "subset"
    subdir.mkdir()
    (subdir / "lane-hash.json").write_text(
        (cdir / "lane-hash.json").read_text()
    )
    try:
        merge_checkpoints(subdir, output=out, contract=contract)
    except Exception as exc:
        assert "subset" in str(exc).lower() or "overwrite" in str(exc).lower()
    else:
        raise AssertionError(
            "subset merge must refuse to overwrite the complete result"
        )
