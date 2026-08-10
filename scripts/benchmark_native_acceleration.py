"""R3 performance gate: native vs pure-Python end-to-end search.

Compares production LocalVectorIndex.search() with backend=python vs
backend=rust at representative 10K scopes, including SQLite row-fetch,
FFI conversion and the full scoring path.

Gates (plan R3 Performance):
- >= 25% end-to-end p95 improvement over the strongest pure-Python
  baseline at representative 10K scopes;
- absolute p50/p95/p99 reported;
- dimensions 384/512/768/1024;
- each lane repeated 3+ times in fresh processes (run via the runner's
  subprocess protocol);
- no material regression for small scopes where FFI may dominate.

Dedicated output files only — never the shared benchmark.log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


def _seed_scope(index: LocalVectorIndex, scope: Scope, n: int, dim: int) -> None:
    memories = []
    for i in range(n):
        content = (
            f"observation {i}: the service at site {i % 23} processed "
            f"{i * 7} requests and logged {i % 5} warnings at {i % 24}:00"
        )
        memories.append(
            Memory(
                id=f"m{i:08d}",
                scope=scope,
                kind="derived",
                content=content,
                source_observation_ids=["obs-1"],
                model_id=f"hash-{dim}",
                prompt_version="v1",
                confidence=0.9,
            )
        )
    index.upsert_many(memories)


def _run_lane(index: LocalVectorIndex, scope: Scope, query: str, reps: int) -> dict[str, float]:
    # warm-up
    index.search(scope, query, limit=20)
    times: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        index.search(scope, query, limit=20)
        times.append((time.perf_counter() - t0) * 1000.0)
    times.sort()
    return {
        "p50": times[len(times) // 2],
        "p95": times[int(len(times) * 0.95) - 1],
        "p99": times[int(len(times) * 0.99) - 1],
        "min": times[0],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="native vs python e2e search gate")
    ap.add_argument("--n", type=int, default=10_000, help="rows in scope")
    ap.add_argument("--dim", type=int, default=384)
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--out", type=Path, required=True, help="dedicated JSON output")
    args = ap.parse_args()

    results: dict[str, Any] = {
        "n": args.n,
        "dim": args.dim,
        "reps": args.reps,
        "queries": ["the service processed requests", "system logged warnings", "site observation"],
        "lanes": {},
    }
    for backend in ("python", "rust"):
        os.environ["SEVERIAN_VECTOR_BACKEND"] = backend
        lane = {}
        for qi, query in enumerate(results["queries"]):
            path = Path(f"/tmp/rem-native-gate-{backend}-{qi}-{os.getpid()}.db")
            index = LocalVectorIndex(path, HashEmbedding(dimension=args.dim))
            scope = Scope(
                tenant_id="t", profile_id="p", collection_id="c",
                agent_id="a", session_id=f"s-{backend}-{qi}",
            )
            _seed_scope(index, scope, args.n, args.dim)
            lane[f"q{qi}"] = _run_lane(index, scope, query, args.reps)
            diag = index.backend_diagnostics()
            lane[f"q{qi}"]["selected"] = diag["selected"]
            lane[f"q{qi}"]["native_version"] = diag["native_version"]
            index.close()
            path.unlink(missing_ok=True)
        results["lanes"][backend] = lane

    # Relative improvement at p95 (python baseline vs rust).
    py_q0 = results["lanes"]["python"]["q0"]["p95"]
    rs_q0 = results["lanes"]["rust"]["q0"]["p95"]
    results["p95_improvement_q0"] = 1.0 - rs_q0 / py_q0
    results["p95_improvement_overall"] = {
        f"q{i}": 1.0 - results["lanes"]["rust"][f"q{i}"]["p95"]
        / results["lanes"]["python"][f"q{i}"]["p95"]
        for i in range(len(results["queries"]))
    }
    args.out.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(json.dumps({k: results[k] for k in ("n", "dim", "p95_improvement_q0", "p95_improvement_overall")}, indent=2))


if __name__ == "__main__":
    main()
