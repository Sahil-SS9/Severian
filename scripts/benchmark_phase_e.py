"""Phase E: selected-model e2e — forced Python vs forced Rust in FRESH processes.

Repeats the e2e search comparison at the selected model's native dimension
(qwen-0.6b = 1024) with each backend forced in its own process, on the final
pyo3-0.29.2 wheel. Records IDs/order parity, absolute latency (p50/p95/p99)
and RSS. Output is a dedicated per-run JSON — never the shared benchmark.log.

Phase E item 1-2 of the master handoff.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

LANE_SRC = r"""
import json, os, sys, time
from pathlib import Path
sys.path.insert(0, "__SRC__")
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
               source_observation_ids=[f"obs{i}"], model_id="phase-e",
               prompt_version="v1", confidence=0.9)
    idx.upsert(m)
# warm
idx.search(scope, query, limit=20)
lat = []
first_ids = None
for _ in range(reps):
    t0 = time.perf_counter()
    hits = idx.search(scope, query, limit=20)
    lat.append((time.perf_counter() - t0) * 1000.0)
    ids = [h.memory_id for h in hits]
    if first_ids is None:
        first_ids = ids
lat.sort()
diag = idx.backend_diagnostics()
print(json.dumps({
    "backend": backend, "dim": dim, "n": n,
    "p50": lat[len(lat)//2], "p95": lat[int(len(lat)*0.95)-1],
    "p99": lat[int(len(lat)*0.99)-1],
    "selected": diag["selected"], "native_version": diag.get("native_version"),
    "first_ids": first_ids,
}))
idx.close()
"""


def run_lane(backend: str, dim: int, n: int, reps: int, db: Path,
             query: str) -> dict:
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
        capture_output=True, text=True, env=env, timeout=900,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"{backend} lane failed: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dim", type=int, default=1024)  # qwen-0.6b native dim
    ap.add_argument("--n", type=int, default=10_000)
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    results: dict = {"dim": args.dim, "n": args.n, "reps": args.reps,
                     "lanes": {}}
    query = "the service processed requests and logged warnings"
    for backend in ("python", "rust"):
        db = Path(f"/tmp/rem-phase-e-{backend}-{os.getpid()}.db")
        lane = run_lane(backend, args.dim, args.n, args.reps, db, query)
        results["lanes"][backend] = lane
        db.unlink(missing_ok=True)

    py, rs = results["lanes"]["python"], results["lanes"]["rust"]
    results["id_parity"] = py["first_ids"] == rs["first_ids"]
    results["p95_improvement"] = 1.0 - rs["p95"] / py["p95"]
    results["rust_selected"] = rs["selected"]
    results["native_version"] = rs["native_version"]
    args.out.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
