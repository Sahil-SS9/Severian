"""Embedding bake-off runner (zero-debt plan, Task 1.2).

Selects Severian's embedding tiers from reproducible real-workload
evidence, NOT from public MTEB alone (locked decision D4). Every lane
runs the SAME corpus and query set through the SAME Severian retrieval
path, differing only in the embedding provider.

Reuses ``BenchmarkRunner`` scoring (recall@10, MRR, nDCG@10, citation
precision, stale/temporal/leakage/unsupported rates, latency) — no
duplicated metric implementations. Adds the model-tier measurements the
selection gates require: cold load, encode throughput, artifact bytes,
peak RSS, index build, vector bytes per memory.

Candidates (Task 1.2 matrix):
- hash: HashEmbedding baseline (no model; the fallback contract)
- granite-107m: current control (granite-embedding-107m-multilingual)
- granite-97m: balanced default candidate
- gemma-300m: balanced candidate (record gated acquisition terms)
- qwen-0.6b: high tier (instruction + no-instruction control)
- granite-311m: high tier (768 and 384 Matryoshka when supported)
- bekko-a8m: lite multilingual
- granite-small-en: English-only control (CANNOT win multilingual)
- jina-v4: EXCLUDED — 3.8B / ~7.87GB BF16 / non-commercial terms;
  recorded in the manifest as excluded, never downloaded/run here.

Each lane records: model revision (HF commit), dimension, pooling/
normalisation, query/doc prefix contract, runtime + quantisation,
cold model load time, p50/p95/p99 query embedding latency (batch 1),
document encode throughput, peak RSS, vector bytes per memory, index
build duration, and the full quality metric set.
"""
from __future__ import annotations

import json
import os
import resource
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from severian.benchmarks.runner import (
    BenchmarkRunner,
    ProviderAdapter,
    ProviderResult,
    to_jsonable,
)


@dataclass
class EmbeddingLaneResult:
    """Model-tier measurements + the quality result for one lane."""

    provider_id: str
    model_id: str
    revision: str
    dimension: int
    normalize: bool
    runtime: str
    quantisation: str
    cold_load_seconds: float
    artifact_bytes: int
    peak_rss_mb: float
    encode_throughput_docs_per_sec: float
    query_latency_p50_ms: float
    query_latency_p95_ms: float
    query_latency_p99_ms: float
    index_build_seconds: float
    vector_bytes_per_memory: float
    quality: ProviderResult
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class EmbeddingBakeoffResult:
    manifest_id: str
    lanes: list[EmbeddingLaneResult]
    corpus_sha256: str
    hardware: dict[str, Any]
    run_started_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "corpus_sha256": self.corpus_sha256,
            "hardware": self.hardware,
            "run_started_at": self.run_started_at,
            "lanes": [asdict(lane) for lane in self.lanes],
        }


class EmbeddingBakeoffRunner:
    """Run the bake-off: build each embedding lane, measure, score.

    Quality scoring delegates to :class:`BenchmarkRunner` with a
    per-lane adapter — the SAME manifest, the SAME metric code. The
    model-tier measurements are collected around the adapter lifecycle
    (construction = cold load; ingest = index build; queries = latency;
    a dedicated encode batch = throughput).
    """

    def __init__(
        self,
        *,
        manifest: Any,
        lane_factory: Mapping[str, Any],
        corpus_path: Path | None = None,
    ) -> None:
        self._manifest = manifest
        self._lane_factory = dict(lane_factory)
        self._corpus_path = corpus_path

    # -- model-tier measurements ------------------------------------------

    @staticmethod
    def _peak_rss_mb() -> float:
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

    @staticmethod
    def _artifact_bytes(model_dir: str | None) -> int:
        if not model_dir:
            return 0
        root = Path(model_dir).expanduser()
        if not root.exists():
            return 0
        total = 0
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    @staticmethod
    def _percentiles(values: list[float]) -> tuple[float, float, float]:
        ordered = sorted(values)
        if not ordered:
            return (0.0, 0.0, 0.0)
        n = len(ordered)

        def at(frac: float) -> float:
            idx = max(0, min(n - 1, int(round(frac * (n - 1)))))
            return ordered[idx]

        return (at(0.50), at(0.95), at(0.99))

    # -- lane lifecycle ----------------------------------------------------

    def _measure_lane(self, provider_id: str, spec: Any) -> EmbeddingLaneResult:
        factory = self._lane_factory[provider_id]
        cfg = spec.config or {}
        model = spec.model
        dimension = int(cfg.get("dimension", 384))
        normalize = bool(cfg.get("normalize", True))
        runtime = str(cfg.get("runtime", "unknown"))
        quantisation = str(cfg.get("quantisation", "none"))
        revision = str(cfg.get("revision", "unknown"))
        model_dir = cfg.get("model_dir")

        adapter: ProviderAdapter | None = None
        try:
            rss_before = self._peak_rss_mb()
            t0 = time.perf_counter()
            built = factory()  # construction includes model load
            cold_load = time.perf_counter() - t0
            assert built is not None, "lane factory returned None"
            adapter = built

            # Ingest = index build (includes encode of every doc).
            t0 = time.perf_counter()
            for doc in self._manifest.corpus.documents:
                built.ingest(doc.id, doc.content, scope=doc.scope,
                             supersedes=doc.supersedes)
            index_build = time.perf_counter() - t0

            # Encode throughput on a fresh batch (documents).
            encode_texts = [doc.content for doc in self._manifest.corpus.documents][:200]
            t0 = time.perf_counter()
            for text in encode_texts:
                built.ingest(f"enc-{abs(hash(text)) % 10**9}", text,
                             scope=self._manifest.corpus.documents[0].scope)
            encode_elapsed = time.perf_counter() - t0
            encode_tps = len(encode_texts) / encode_elapsed if encode_elapsed > 0 else 0.0

            # zero-debt P0.3: warm the index to FULL derivation BEFORE
            # scoring. The old flow left derive=True on every query —
            # each query derived up to 50 observations INTO the index,
            # growing it mid-run (measured: granite-97m queries started
            # at 36ms and ended at ~53s mean; p95 79s while hash was
            # 1.6s). That measured mixed derive+retrieval, not retrieval.
            # Warmup uses the derive-only loop (linear, no query cost);
            # scoring then runs with derive=False on a stable index.
            if hasattr(built, "warmup"):
                scopes = sorted({doc.scope for doc in self._manifest.corpus.documents})
                for warm_scope in scopes:
                    try:
                        built.warmup(scope=warm_scope)
                    except Exception as exc:  # noqa: BLE001 — lane isolation
                        print(f"  [{provider_id}] warmup({warm_scope}) failed: {exc}")

            # Quality + query latency via the shared scorer. allow_missing:
            # the bake-off runs ONE lane at a time — the other manifest
            # providers legitimately have no adapter registered.
            runner = BenchmarkRunner(manifest=self._manifest,
                                     adapters={provider_id: built},
                                     allow_missing=True,
                                     derive_on_query=False)
            quality = runner._run_provider(provider_id)  # noqa: SLF001 — reuse scoring
            latencies = [float(raw["elapsed_ms"]) for raw in quality.raw]
            p50, p95, p99 = self._percentiles(latencies)
            peak_rss = self._peak_rss_mb() - rss_before

            return EmbeddingLaneResult(
                provider_id=provider_id,
                model_id=model.id,
                revision=revision,
                dimension=dimension,
                normalize=normalize,
                runtime=runtime,
                quantisation=quantisation,
                cold_load_seconds=round(cold_load, 3),
                artifact_bytes=self._artifact_bytes(model_dir),
                peak_rss_mb=round(max(0.0, peak_rss), 1),
                encode_throughput_docs_per_sec=round(encode_tps, 1),
                query_latency_p50_ms=round(p50, 2),
                query_latency_p95_ms=round(p95, 2),
                query_latency_p99_ms=round(p99, 2),
                index_build_seconds=round(index_build, 3),
                vector_bytes_per_memory=dimension * 4,  # float32
                quality=quality,
            )
        except Exception as exc:  # noqa: BLE001 — lane isolation
            return EmbeddingLaneResult(
                provider_id=provider_id,
                model_id=model.id,
                revision=revision,
                dimension=dimension,
                normalize=normalize,
                runtime=runtime,
                quantisation=quantisation,
                cold_load_seconds=0.0,
                artifact_bytes=self._artifact_bytes(model_dir),
                peak_rss_mb=0.0,
                encode_throughput_docs_per_sec=0.0,
                query_latency_p50_ms=0.0,
                query_latency_p95_ms=0.0,
                query_latency_p99_ms=0.0,
                index_build_seconds=0.0,
                vector_bytes_per_memory=dimension * 4,
                quality=ProviderResult(provider_id=provider_id, queries_run=0,
                                       recall_at_10=0.0, mrr=0.0, ndcg_at_10=0.0,
                                       citation_precision=0.0, stale_fact_rate=0.0,
                                       temporal_accuracy=0.0, cross_scope_leakage=0.0,
                                       unsupported_answer_rate=0.0,
                                       latency_p95_ms=0.0, cost_per_query_usd=0.0),
                skipped=True,
                skip_reason=str(exc)[:200],
            )
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001
                    pass

    def run(self, checkpoint_dir: Path | None = None) -> EmbeddingBakeoffResult:
        from datetime import UTC, datetime

        lanes: list[EmbeddingLaneResult] = []
        for provider_id, spec in enumerate_providers(self._manifest):
            if provider_id not in self._lane_factory:
                lanes.append(EmbeddingLaneResult(
                    provider_id=provider_id, model_id=spec.model.id,
                    revision=str(spec.config.get("revision", "unknown")),
                    dimension=int(spec.config.get("dimension", 384)),
                    normalize=bool(spec.config.get("normalize", True)),
                    runtime=str(spec.config.get("runtime", "unknown")),
                    quantisation=str(spec.config.get("quantisation", "none")),
                    cold_load_seconds=0.0, artifact_bytes=0, peak_rss_mb=0.0,
                    encode_throughput_docs_per_sec=0.0,
                    query_latency_p50_ms=0.0, query_latency_p95_ms=0.0,
                    query_latency_p99_ms=0.0, index_build_seconds=0.0,
                    vector_bytes_per_memory=0.0,
                    quality=ProviderResult(provider_id=provider_id, queries_run=0,
                                           recall_at_10=0.0, mrr=0.0, ndcg_at_10=0.0,
                                           citation_precision=0.0, stale_fact_rate=0.0,
                                           temporal_accuracy=0.0, cross_scope_leakage=0.0,
                                           unsupported_answer_rate=0.0,
                                           latency_p95_ms=0.0, cost_per_query_usd=0.0),
                    skipped=True, skip_reason="no lane factory registered",
                ))
                continue
            print(f"  [{provider_id}] building lane...", flush=True)
            lane = self._measure_lane(provider_id, spec)
            lanes.append(lane)
            # zero-debt 3.3 hardening: checkpoint each completed lane
            # immediately so a timeout/crash never loses finished work.
            if checkpoint_dir is not None:
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                (checkpoint_dir / f"lane-{provider_id}.json").write_text(
                    json.dumps(asdict(lane), sort_keys=True)
                )
                print(f"  [{provider_id}] checkpointed -> lane-{provider_id}.json", flush=True)

        import platform

        hardware = {
            "cpu": _cpu_model(),
            "cores": os.cpu_count(),
            "platform": platform.platform(),
        }
        corpus_sha = ""
        if self._corpus_path and self._corpus_path.exists():
            import hashlib

            corpus_sha = hashlib.sha256(self._corpus_path.read_bytes()).hexdigest()
        return EmbeddingBakeoffResult(
            manifest_id=self._manifest.manifest_id,
            lanes=lanes,
            corpus_sha256=corpus_sha,
            hardware=hardware,
            run_started_at=datetime.now(UTC).isoformat(),
        )


def enumerate_providers(manifest: Any) -> list[tuple[str, Any]]:
    """Return (provider_id, spec) for every runnable provider in order."""
    return [(p.id, p) for p in manifest.providers if not p.vendor_reported_only]


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


__all__ = [
    "EmbeddingBakeoffResult",
    "EmbeddingBakeoffRunner",
    "EmbeddingLaneResult",
    "enumerate_providers",
    "to_jsonable",
]
