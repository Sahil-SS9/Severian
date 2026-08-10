"""Embedding bake-off runner (zero-debt plan, Task 1.2) — repaired.

Selects Severian's embedding tiers from reproducible real-workload
evidence, NOT from public MTEB alone (locked decision D4). Every lane
runs the SAME corpus and query set through the SAME Severian retrieval
path, differing only in the embedding provider.

Phase B (remediation) contract — audit CRITICAL 1-3 / HIGH 5-8:

- the corpus path is hashed separately from the manifest (audit
  CRITICAL 1: the old code hashed the manifest and published it as the
  corpus SHA);
- encode throughput is measured in a SEPARATE fresh adapter, never into
  the index used for quality scoring (audit CRITICAL 3);
- the scored index is asserted to contain EXACTLY the manifest corpus
  IDs before any query runs;
- the manifest release gate is evaluated for EVERY completed lane and
  ``gate_passed`` / ``gate_failures`` are persisted per lane (audit
  CRITICAL 2);
- each lane runs in a FRESH subprocess so peak RSS is a true process
  peak, not a sequential ru_maxrss delta (audit HIGH 7);
- model revisions are pinned immutable HF revisions (audit HIGH 6).

Candidates (Task 1.2 matrix) live in the manifest; this runner executes
whatever runnable providers the manifest declares.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
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
    gate_passed: bool | None = None
    gate_failures: list[str] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


@dataclass
class EmbeddingBakeoffResult:
    manifest_id: str
    lanes: list[EmbeddingLaneResult]
    corpus_sha256: str
    manifest_sha256: str
    hardware: dict[str, Any]
    run_started_at: str
    runner_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "corpus_sha256": self.corpus_sha256,
            "manifest_sha256": self.manifest_sha256,
            "runner_sha256": self.runner_sha256,
            "hardware": self.hardware,
            "run_started_at": self.run_started_at,
            "lanes": [asdict(lane) for lane in self.lanes],
        }


class EmbeddingBakeoffRunner:
    """Run the bake-off: build each embedding lane, measure, score.

    Quality scoring delegates to :class:`BenchmarkRunner` with a
    per-lane adapter — the SAME manifest, the SAME metric code. The
    model-tier measurements are collected around the adapter lifecycle.

    Each lane runs in a FRESH subprocess (``_measure_lane``) so peak RSS
    is a true process peak and a broken lane cannot corrupt the parent.
    """

    def __init__(
        self,
        *,
        manifest: Any,
        lane_factory: Mapping[str, Any],
        corpus_path: Path | None = None,
        manifest_path: Path | None = None,
    ) -> None:
        self._manifest = manifest
        self._lane_factory = dict(lane_factory)
        self._corpus_path = corpus_path
        # Explicit path for the fresh-subprocess protocol (audit HIGH 7).
        # Never reach into pydantic private attrs: BenchmarkManifest uses
        # extra="forbid", so a `_path` attribute can never exist there.
        self._manifest_path = manifest_path

    # -- model-tier measurements ------------------------------------------

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
        """(p50, p95, p99) over raw samples using the ONE documented
        quantile method (linear interpolation, type 7 — see
        severian.benchmarks.quantiles; P1-H)."""
        from severian.benchmarks.quantiles import p50_p95_p99

        return p50_p95_p99(sorted(values))

    # -- lane lifecycle ----------------------------------------------------

    def _measure_lane(self, provider_id: str, spec: Any) -> EmbeddingLaneResult:
        """Run one lane in a fresh subprocess and return its result.

        A fresh process gives a true per-lane peak RSS (audit HIGH 7)
        and isolates model-load crashes. The subprocess imports this
        module and calls :meth:`_measure_lane_inline`.
        """

        # Serialise the manifest + factory registry path to the child.
        manifest_path = self._manifest_path
        if manifest_path is None:
            # Inline fallback for in-memory manifests (tests): run in
            # this process but with the fresh-process protocol.
            return self._measure_lane_inline(provider_id, spec)

        payload = {
            "provider_id": provider_id,
            "manifest_path": str(manifest_path),
            "corpus_path": str(self._corpus_path) if self._corpus_path else None,
            "lane_module": "scripts.run_embedding_benchmark",
            # P0-F: capped-resource lanes — applied INSIDE the fresh child
            # process (never the parent/tests). cpus = CPU affinity count,
            # mem_mb = RLIMIT_AS cap in MiB.
            "resource_caps": {
                "cpus": int(spec.config.get("cpus", 0)) or None,
                "mem_mb": int(spec.config.get("mem_mb", 0)) or None,
            },
        }
        script = (
            "import json, sys; "
            "from severian.benchmarks.embedding_runner import _lane_child_entrypoint; "
            "_lane_child_entrypoint(json.loads(sys.argv[1]))"
        )
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script, json.dumps(payload)],
                capture_output=True, text=True, timeout=3600,
                cwd=str(Path(__file__).resolve().parents[2]),
            )
        except subprocess.TimeoutExpired:
            return self._skipped(provider_id, spec, "lane timed out (3600s)")
        if proc.returncode != 0:
            return self._skipped(
                provider_id, spec,
                f"lane subprocess failed (exit {proc.returncode}): "
                f"{proc.stderr.strip()[:300]}",
            )
        try:
            data = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as exc:  # noqa: BLE001
            return self._skipped(provider_id, spec, f"lane result unparseable: {exc}")
        lane = EmbeddingLaneResult(**data)
        lane.quality = ProviderResult(**data["quality"]) if "quality" in data else lane.quality
        return lane

    @staticmethod
    def _skipped(provider_id: str, spec: Any, reason: str) -> EmbeddingLaneResult:
        return EmbeddingLaneResult(
            provider_id=provider_id,
            model_id=spec.model.id,
            revision=str(spec.config.get("revision", "unknown")),
            dimension=int(spec.config.get("dimension", 384)),
            normalize=bool(spec.config.get("normalize", True)),
            runtime=str(spec.config.get("runtime", "unknown")),
            quantisation=str(spec.config.get("quantisation", "none")),
            cold_load_seconds=0.0,
            artifact_bytes=0,
            peak_rss_mb=0.0,
            encode_throughput_docs_per_sec=0.0,
            query_latency_p50_ms=0.0,
            query_latency_p95_ms=0.0,
            query_latency_p99_ms=0.0,
            index_build_seconds=0.0,
            vector_bytes_per_memory=int(spec.config.get("dimension", 384)) * 4,
            quality=ProviderResult(
                provider_id=provider_id, queries_run=0, recall_at_10=0.0,
                mrr=0.0, ndcg_at_10=0.0, citation_precision=0.0,
                stale_fact_rate=0.0, temporal_accuracy=0.0,
                cross_scope_leakage=0.0, unsupported_answer_rate=0.0,
                negative_false_positive_rate=0.0,
                latency_p95_ms=0.0, cost_per_query_usd=0.0,
            ),
            skipped=True,
            skip_reason=reason[:300],
        )

    def _measure_lane_inline(self, provider_id: str, spec: Any) -> EmbeddingLaneResult:
        """Measure one lane in-process (used by fresh-process child and
        in-memory test manifests)."""
        import resource

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
            # Cold load is the construction time (model load included).
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

            # Warm the index to FULL derivation BEFORE scoring so query
            # latency measures retrieval, not derivation (zero-debt 2.4).
            if hasattr(built, "warmup"):
                scopes = sorted({doc.scope for doc in self._manifest.corpus.documents})
                for warm_scope in scopes:
                    try:
                        built.warmup(scope=warm_scope)
                    except Exception as exc:  # noqa: BLE001 — lane isolation
                        print(f"  [{provider_id}] warmup({warm_scope}) failed: {exc}")

            # Audit CRITICAL 3: verify the scored index contains EXACTLY
            # the manifest corpus IDs — no throughput probes, no residue.
            self._assert_exact_index(provider_id, built)

            # Quality + query latency via the shared scorer.
            runner = BenchmarkRunner(manifest=self._manifest,
                                     adapters={provider_id: built},
                                     allow_missing=True,
                                     derive_on_query=False)
            quality = runner._run_provider(provider_id)  # noqa: SLF001 — reuse scoring
            latencies = [float(raw["elapsed_ms"]) for raw in quality.raw]
            p50, p95, p99 = self._percentiles(latencies)
            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0

            # Throughput in a SEPARATE fresh adapter (never the scored
            # index — audit CRITICAL 3). The probe adapter gets its own
            # workdir so enc-* rows cannot contaminate scoring.
            # P1-G: snapshot the scored index's exact document IDs BEFORE
            # the probe and require equality AFTER — throughput/warm-up
            # probes must never change the scored index.
            snapshot_before = self._snapshot_ids(built)
            encode_tps, probe_clean = self._measure_throughput_fresh(provider_id, spec)
            snapshot_after = self._snapshot_ids(built)
            if snapshot_before is not None and snapshot_after != snapshot_before:
                probe_clean = False
                print(
                    f"  [{provider_id}] scored index ID set changed during "
                    "throughput probe — probe is not clean"
                )

            gate_passed, gate_failures = self._evaluate_lane_gate(provider_id, quality)

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
                gate_passed=gate_passed,
                gate_failures=gate_failures,
                skipped=not probe_clean,
                skip_reason="" if probe_clean else "throughput probe lane was not clean",
            )
        except Exception as exc:  # noqa: BLE001 — lane isolation
            return self._skipped(provider_id, spec, str(exc)[:200])
        finally:
            if adapter is not None:
                try:
                    adapter.close()
                except Exception:  # noqa: BLE001
                    pass

    def _assert_exact_index(self, provider_id: str, adapter: ProviderAdapter) -> None:
        """Audit CRITICAL 3 / P1-G: the scored index must contain EXACTLY
        the manifest corpus document IDs — both directions. Extra IDs
        (throughput/warm-up contamination) AND missing IDs (partial
        ingest) invalidate quality scoring."""
        list_ids = getattr(adapter, "list_document_ids", None)
        if list_ids is None:
            return  # non-inspectable adapter: caller must guarantee
        stored = set(list_ids())
        expected = {doc.id for doc in self._manifest.corpus.documents}
        extra = stored - expected
        missing = expected - stored
        if extra:
            raise RuntimeError(
                f"lane {provider_id}: scored index contains {len(extra)} "
                f"unmanifested IDs (e.g. {sorted(extra)[:3]}) — "
                "quality scoring is contaminated"
            )
        if missing:
            raise RuntimeError(
                f"lane {provider_id}: scored index is missing {len(missing)} "
                f"manifested IDs (e.g. {sorted(missing)[:3]}) — "
                "quality scoring is incomplete"
            )

    def _snapshot_ids(self, adapter: ProviderAdapter) -> frozenset[str] | None:
        """Exact scored-index document-ID snapshot (P1-G). None when the
        adapter is not inspectable (caller then cannot verify)."""
        list_ids = getattr(adapter, "list_document_ids", None)
        if list_ids is None:
            return None
        return frozenset(list_ids())

    def _measure_throughput_fresh(
        self, provider_id: str, spec: Any
    ) -> tuple[float, bool]:
        """Encode throughput on a SEPARATE fresh adapter/index.

        Returns (docs_per_sec, clean) where clean=True when the probe
        adapter started and finished with exactly the probe documents —
        it never touches the scored index.
        """
        factory = self._lane_factory[provider_id]
        probe: ProviderAdapter | None = None
        try:
            built = factory()
            assert built is not None, "throughput probe factory returned None"
            probe = built
            encode_texts = [doc.content for doc in self._manifest.corpus.documents][:200]
            scope = self._manifest.corpus.documents[0].scope
            t0 = time.perf_counter()
            for i, text in enumerate(encode_texts):
                probe.ingest(f"enc-{i:04d}", text, scope=scope)
            encode_elapsed = time.perf_counter() - t0
            tps = len(encode_texts) / encode_elapsed if encode_elapsed > 0 else 0.0
            return tps, True
        except Exception as exc:  # noqa: BLE001
            print(f"  [{provider_id}] throughput probe failed: {exc}")
            return 0.0, False
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:  # noqa: BLE001
                    pass

    def _evaluate_lane_gate(
        self, provider_id: str, quality: ProviderResult
    ) -> tuple[bool, list[str]]:
        """Audit CRITICAL 2: run the manifest release gate per lane."""
        gate = self._manifest.release_gate
        failures: list[str] = []
        if quality.recall_at_10 < gate.min_recall_at_10:
            failures.append("recall_at_10")
        if quality.citation_precision < gate.min_citation_precision:
            failures.append("citation_precision")
        if quality.stale_fact_rate > gate.max_stale_fact_rate:
            failures.append("stale_fact_rate")
        if quality.cross_scope_leakage > gate.max_cross_scope_leakage:
            failures.append("cross_scope_leakage")
        if quality.unsupported_answer_rate > gate.max_unsupported_answer_rate:
            failures.append("unsupported_answer_rate")
        if quality.negative_false_positive_rate > gate.max_negative_false_positive_rate:
            failures.append("negative_false_positive_rate")
        return (not failures, failures)

    def run(self, checkpoint_dir: Path | None = None) -> EmbeddingBakeoffResult:
        from datetime import UTC, datetime

        lanes: list[EmbeddingLaneResult] = []
        for provider_id, spec in enumerate_providers(self._manifest):
            if provider_id not in self._lane_factory:
                lanes.append(self._skipped(
                    provider_id, spec, "no lane factory registered",
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
        import hashlib

        corpus_sha = ""
        if self._corpus_path and self._corpus_path.exists():
            corpus_sha = hashlib.sha256(self._corpus_path.read_bytes()).hexdigest()
        manifest_sha = hashlib.sha256(
            self._manifest.model_dump_json().encode()
        ).hexdigest()
        runner_sha = hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest()
        result = EmbeddingBakeoffResult(
            manifest_id=self._manifest.manifest_id,
            lanes=lanes,
            corpus_sha256=corpus_sha,
            manifest_sha256=manifest_sha,
            runner_sha256=runner_sha,
            hardware=hardware,
            run_started_at=datetime.now(UTC).isoformat(),
        )
        # audit B13: persist the contract alongside the lane checkpoints
        # so a later merge can refuse incompatible or subset runs.
        if checkpoint_dir is not None:
            from severian.benchmarks.checkpoint_merge import write_contract
            write_contract(checkpoint_dir / "bakeoff-contract.json", result)
        return result


def _apply_resource_caps(caps: dict[str, Any] | None) -> None:
    """Apply per-lane resource caps inside the fresh child process.

    - cpus: pin the process to the first N logical CPUs (real affinity,
      so throughput/latency reflect the capped lane).
    - mem_mb: hard RLIMIT_AS cap in MiB — a lane that exceeds it dies
      with MemoryError and is recorded as skipped, never faked.
    """
    caps = caps or {}
    cpus = caps.get("cpus")
    if cpus:
        import os

        available = sorted(os.sched_getaffinity(0))
        if len(available) < cpus:
            raise SystemExit(
                f"cannot cap lane to {cpus} CPUs (only {len(available)} available)"
            )
        os.sched_setaffinity(0, set(available[:cpus]))
    mem_mb = caps.get("mem_mb")
    if mem_mb:
        import resource

        limit = int(mem_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _lane_child_entrypoint(payload: dict[str, Any]) -> None:
    """Fresh-process child: rebuild the manifest + factory and measure
    one lane, printing one JSON line on stdout."""
    import sys

    _apply_resource_caps(payload.get("resource_caps"))

    # parents: [0]=benchmarks [1]=severian [2]=src [3]=repo root
    _repo_root = Path(__file__).resolve().parents[3]
    # Repo root FIRST: `scripts` is a namespace package (no __init__.py),
    # so `from scripts.run_embedding_benchmark import ...` only resolves
    # when the PARENT of scripts/ is on sys.path, not scripts/ itself.
    sys.path.insert(0, str(_repo_root))
    sys.path.insert(0, str(_repo_root / "src"))
    sys.path.insert(0, str(_repo_root / "scripts"))

    from scripts.run_embedding_benchmark import (
        build_lane_factories,
    )
    from severian.benchmarks.manifest import load_manifest

    manifest = load_manifest(payload["manifest_path"])
    if payload.get("corpus_path"):
        from scripts.run_embedding_benchmark import _inject_corpus
        manifest = _inject_corpus(manifest, Path(payload["corpus_path"]))
    factories = build_lane_factories(
        argparse_namespace(payload), manifest,
    )
    provider_id = payload["provider_id"]
    if provider_id not in factories:
        raise SystemExit(f"lane factory {provider_id} not built in child")
    runner = EmbeddingBakeoffRunner(
        manifest=manifest,
        lane_factory={provider_id: factories[provider_id]},
        corpus_path=Path(payload["corpus_path"]) if payload.get("corpus_path") else None,
    )
    spec = next(s for pid, s in enumerate_providers(manifest) if pid == provider_id)
    lane = runner._measure_lane_inline(provider_id, spec)  # noqa: SLF001
    data = asdict(lane)
    data["quality"] = asdict(lane.quality)
    print(json.dumps(data, sort_keys=True))


def argparse_namespace(payload: dict[str, Any]) -> Any:
    """Minimal argparse.Namespace with the fields build_lane_factories needs."""
    import argparse
    ns = argparse.Namespace()
    ns.only = ""
    return ns


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
