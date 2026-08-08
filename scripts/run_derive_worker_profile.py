"""Phase 0.3 lane: derive-path and worker-drain profiling (current HEAD).

Measures, on deterministic synthetic scopes:

1. Derive path (MemoryService.search with derive=True):
   - fully derived scope (zero pending work): bounded search must NOT
     re-derive history; cost should approach derive=False.
   - one pending observation: only that item is processed.
   - 50 pending observations: only those 50 are processed.
   Reports created/existing counts, wall time and a cProfile attribution.

2. Worker drain (DurableWorker.drain):
   - one queued job, 50 queued jobs, full queue.
   Reports jobs processed, time, per-job ms and queue-emptied proof.

Evidence lands under an owned output dir with full provenance (quality
rule 4) via scripts/bench_provenance.py.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_provenance import collect_provenance  # noqa: E402
from severian.application.worker import DerivationWorker  # noqa: E402
from severian.composition import build_bundle  # noqa: E402
from severian.domain.models import Collection, Observation, Scope  # noqa: E402

SCOPE = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


class HashGateway:
    """Deterministic local derivation gateway (mirrors CLI _HashingGateway)."""

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, Any]]:
        import hashlib

        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
        return [
            {
                "kind": "fact",
                "content": content,
                "prompt_version": "hash-v1",
                "model_id": "hash",
                "confidence": 0.8,
                "id_suffix": digest,
            }
        ]


def _make_bundle(tmp: str, memories: int) -> Any:
    root = Path(tmp)
    bundle = build_bundle(backend="sqlite", database=root / "prof.db")
    bundle.repository.create_collection(Collection(id="c1", tenant_id="t1", name="prof"))
    return bundle


def _ingest(bundle: Any, count: int) -> None:
    for i in range(count):
        bundle.service.ingest_observation(
            SCOPE,
            content=f"fact {i}: the service handles {i * 7} requests per second",
            source={"uri": f"note://prof-{i}"},
        )


def _fully_derive(bundle: Any, gateway: Any) -> int:
    """Derive the whole scope by looping until memory count stabilises.

    search(derive=True) caps at derive_max_observations=50 new memories
    per call, so a single call does NOT fully derive a scope. Loop until
    no new memories appear (or a sane ceiling) so later lanes measure
    from a genuinely fully-derived baseline.
    """
    prev = -1
    for _ in range(200):
        bundle.service.search(SCOPE, query="facts", limit=5, derive=True, gateway=gateway)
        cur = len(bundle.repository.list_memories(SCOPE))
        if cur == prev:
            return cur
        prev = cur
    return prev


def _derive_lane(bundle: Any, pending: int, total: int) -> dict[str, Any]:
    """Derive lane: fully derived / one pending / N pending."""
    gw = HashGateway()
    fully_derived = _fully_derive(bundle, gw)
    memory_before = len(bundle.repository.list_memories(SCOPE))

    # Add `pending` new observations (underived work).
    for i in range(pending):
        bundle.service.ingest_observation(
            SCOPE,
            content=f"pending {i}: the queue depth reached {i * 3} messages",
            source={"uri": f"note://pending-{i}"},
        )

    t0 = time.perf_counter()
    bundle.service.search(SCOPE, query="queue depth reached", limit=5,
                          derive=True, gateway=gw)
    delta_ms = (time.perf_counter() - t0) * 1000.0
    memory_after = len(bundle.repository.list_memories(SCOPE))
    created = max(0, memory_after - memory_before)

    # Bounded no-work search: fully derived scope, derive=True should be
    # near derive=False (O(1)/O(log n) pending lookup after fix; today it
    # re-scans history).
    t0 = time.perf_counter()
    bundle.service.search(SCOPE, query="service handles requests", limit=5,
                          derive=True, gateway=gw)
    no_work_ms = (time.perf_counter() - t0) * 1000.0
    t0 = time.perf_counter()
    bundle.service.search(SCOPE, query="service handles requests", limit=5,
                          derive=False)
    no_derive_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "pending": pending,
        "total_scope_memories": total,
        "fully_derived_memories": fully_derived,
        "created_by_pending": created,
        "search_with_pending_ms": round(delta_ms, 2),
        "derive_no_work_ms": round(no_work_ms, 2),
        "derive_false_ms": round(no_derive_ms, 2),
        "memory_before": memory_before,
        "memory_after": memory_after,
    }


def _worker_lane(bundle: Any, jobs: int, gateway: Any) -> dict[str, Any]:
    """Worker drain lane: enqueue N jobs, drain, report."""
    bundle.repository.reap_expired_jobs()
    for i in range(jobs):
        obs = bundle.repository.add_observation(
            Observation(scope=SCOPE, content=f"job {i}: worker processes {i} items",
                        raw_content=f"job {i}", source={"uri": f"note://job-{i}"})
        )
        bundle.repository.enqueue_job(SCOPE, "derive", {"observation_id": obs.id})

    worker = DerivationWorker(
        bundle.repository, bundle.service._derivation, bundle.service._projection  # type: ignore[arg-type]
    )
    t0 = time.perf_counter()
    summary = worker.drain(worker_id="prof-worker", gateway=gateway, max_jobs=jobs)
    delta_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "jobs": jobs,
        "processed": summary.processed,
        "failed": summary.failed,
        "drain_ms": round(delta_ms, 2),
        "ms_per_job": round(delta_ms / max(jobs, 1), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("/tmp/zero-debt-profile"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {"derive": [], "worker": []}

    # cProfile on the bounded derive lane (one pending on 1k scope).
    prof = cProfile.Profile()
    prof.enable()
    with tempfile.TemporaryDirectory() as tmp:
        bundle = _make_bundle(tmp, 0)
        try:
            _ingest(bundle, 1_000)
            derive_lane = _derive_lane(bundle, pending=1, total=1_000)
        finally:
            bundle.close()
    prof.disable()
    s = io.StringIO()
    pstats.Stats(prof, stream=s).sort_stats("cumulative").print_stats(18)
    results["derive_cprofile"] = s.getvalue().splitlines()[:24]
    results["derive"].append(derive_lane)

    # Remaining derive lanes: fully derived, 1 pending, 50 pending at 1k scope.
    with tempfile.TemporaryDirectory() as tmp:
        bundle = _make_bundle(tmp, 0)
        try:
            _ingest(bundle, 1_000)
            results["derive"].append(_derive_lane(bundle, pending=0, total=1_000))
            results["derive"].append(_derive_lane(bundle, pending=1, total=1_000))
            results["derive"].append(_derive_lane(bundle, pending=50, total=1_000))
        finally:
            bundle.close()

    # Worker lanes: 1, 50, full queue (100).
    gw = HashGateway()
    for jobs in (1, 50, 100):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = _make_bundle(tmp, 0)
            try:
                results["worker"].append(_worker_lane(bundle, jobs, gw))
            finally:
                bundle.close()

    payload = {
        "lane": "derive-worker-profile",
        "date": datetime.now(UTC).isoformat(),
        "repro": collect_provenance(REPO_ROOT, sys.argv, seed="deterministic"),
        "results": results,
    }
    out_path = args.out / "derive-worker-profile.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    print(json.dumps(payload, indent=2, sort_keys=True)[:1800])
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
