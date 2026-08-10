"""Vector-only Rust proof: Python vs Rust on identical stored vectors.

REPLACES scripts/benchmark_phase_e.py (which called HashEmbedding but
was labelled "selected-model e2e" — that claim was invalid; P1-H).

This script is STRICTLY vector-only evidence:
- identical stored vectors (hash embedding of the given dimension);
- representative dimensions 384 / 512 / 768 / 1024;
- each (backend, dimension) lane in a FRESH subprocess;
- raw per-query timing samples stored; p50/p95/p99 recomputed with the
  documented linear-interpolation quantile method;
- ID/order parity AND bit-exact score parity between python and rust;
- exact candidate/source identity: git SHA + clean flag + harness hash
  + native wheel version + backend diagnostics.

It is NOT selected-model end-to-end evidence. A model lane must
instantiate the actual pinned model and prompt/dimension contract; a
hash surrogate proves nothing about a model (P1-H).

Promotion thresholds (locked, never lowered):
- kernel speed-up >= 3x (measured separately by the native crate tests);
- end-to-end p95 improvement >= 25%;
- zero ranking/score differences.
Failure means honest defer/reject, never a threshold change.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

LANE_SRC = r"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, "__SRC__")
from severian.benchmarks.quantiles import percentile
from severian.infrastructure.vector import LocalVectorIndex, HashEmbedding
from severian.domain.models import Memory, Scope

backend = os.environ["SEVERIAN_VECTOR_BACKEND"]
dim = int(os.environ["SEVERIAN_LANE_DIM"])
n = int(os.environ["SEVERIAN_LANE_N"])
reps = int(os.environ["SEVERIAN_LANE_REPS"])
db = os.environ["SEVERIAN_LANE_DB"]
query = os.environ["SEVERIAN_LANE_QUERY"]

idx = LocalVectorIndex(Path(db), HashEmbedding(dimension=dim))
scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
for i in range(n):
    m = Memory(id=f"m{i}", content=f"row {i}: the service handled {i} requests "
               f"and logged warning w{i} for scope tenant {i % 7}",
               kind="observation", scope=scope,
               source_observation_ids=[f"obs{i}"], model_id="vector-proof",
               prompt_version="v1", confidence=0.9)
    idx.upsert(m)
# warm (excluded from samples)
idx.search(scope, query, limit=20)
raw = []
hit_ids = []
hit_scores = []
for _ in range(reps):
    t0 = time.perf_counter()
    hits = idx.search(scope, query, limit=20)
    raw.append((time.perf_counter() - t0) * 1000.0)
    if not hit_ids:
        hit_ids = [h.memory_id for h in hits]
        hit_scores = [h.score for h in hits]
ordered = sorted(raw)
diag = idx.backend_diagnostics()
print(json.dumps({
    "backend": backend, "dim": dim, "n": n,
    "raw_ms": [round(v, 6) for v in raw],
    "p50": round(percentile(ordered, 50.0), 4),
    "p95": round(percentile(ordered, 95.0), 4),
    "p99": round(percentile(ordered, 99.0), 4),
    "min": round(ordered[0], 4),
    "selected": diag["selected"], "native_version": diag.get("native_version"),
    "fallback_reason": diag.get("fallback_reason"),
    "hit_ids": hit_ids, "hit_scores": hit_scores,
}))
idx.close()
"""


def run_lane(backend: str, dim: int, n: int, reps: int, db: Path,
             query: str) -> dict[str, Any]:
    env = dict(os.environ)
    env.update({
        "SEVERIAN_VECTOR_BACKEND": backend,
        "SEVERIAN_LANE_DIM": str(dim),
        "SEVERIAN_LANE_N": str(n),
        "SEVERIAN_LANE_REPS": str(reps),
        "SEVERIAN_LANE_DB": str(db),
        "SEVERIAN_LANE_QUERY": query,
    })
    proc = subprocess.run(
        [sys.executable, "-c", LANE_SRC.replace("__SRC__", str(REPO_ROOT / "src"))],
        capture_output=True, text=True, env=env, timeout=1800,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{backend} lane failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _git_identity() -> dict[str, str]:
    try:
        sha = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "status", "--porcelain=v1",
             "--untracked-files=all"],
            capture_output=True, text=True, check=True, timeout=30,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        return {"git_sha": "unknown", "clean": "false", "note": str(exc)}
    return {
        "git_sha": sha,
        "clean": "true" if not dirty else "false",
        "dirty_patch": dirty[:500],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="vector-only rust proof (P1-H)")
    ap.add_argument("--dims", type=int, nargs="+", default=[384, 512, 768, 1024])
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    query = "the service processed requests and logged warnings"
    results: dict[str, Any] = {
        "kind": "vector-only-rust-proof",
        "n": args.n,
        "reps": args.reps,
        "query": query,
        "quantile_method": "linear-interpolation (type 7) over raw_ms",
        "source": _git_identity(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "dims": {},
    }
    for dim in args.dims:
        lanes = {}
        for backend in ("python", "rust"):
            db = Path(f"/tmp/rem-vecproof-{backend}-{dim}-{os.getpid()}.db")
            lanes[backend] = run_lane(backend, dim, args.n, args.reps, db, query)
            db.unlink(missing_ok=True)
        py, rs = lanes["python"], lanes["rust"]
        results["dims"][str(dim)] = {
            "lanes": lanes,
            "id_parity": py["hit_ids"] == rs["hit_ids"],
            "order_parity": py["hit_ids"] == rs["hit_ids"],
            "score_parity": py["hit_scores"] == rs["hit_scores"],
            "max_score_diff": max(
                (abs(a - b) for a, b in zip(py["hit_scores"], rs["hit_scores"], strict=False)),
                default=0.0,
            ),
            "p95_improvement": 1.0 - rs["p95"] / py["p95"],
            "kernel_speedup_3x_gate": "see native crate benchmarks (separate)",
            "e2e_25pct_gate": (1.0 - rs["p95"] / py["p95"]) >= 0.25,
            "zero_difference_gate": (
                py["hit_ids"] == rs["hit_ids"]
                and py["hit_scores"] == rs["hit_scores"]
            ),
        }
    args.out.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(json.dumps({
        "kind": results["kind"],
        "source": results["source"],
        "summary": {
            str(dim): {
                "p95_improvement": results["dims"][str(dim)]["p95_improvement"],
                "id_parity": results["dims"][str(dim)]["id_parity"],
                "score_parity": results["dims"][str(dim)]["score_parity"],
                "e2e_25pct_gate": results["dims"][str(dim)]["e2e_25pct_gate"],
            }
            for dim in args.dims
        },
    }, indent=2))


if __name__ == "__main__":
    main()
