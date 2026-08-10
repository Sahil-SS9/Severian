"""R3 performance gate: native vs pure-Python end-to-end search.

Compares production LocalVectorIndex.search() with backend=python vs
backend=rust at representative 10K scopes, including SQLite row-fetch,
FFI conversion and the full scoring path.

Gates (plan R3 Performance):
- >= 25% end-to-end p95 improvement over the strongest pure-Python
  baseline at representative 10K scopes;
- absolute p50/p95/p99 reported (documented linear-interpolation
  quantiles over stored RAW samples — recomputable, P1-H);
- dimensions 384/512/768/1024;
- each lane repeated 3+ times in fresh processes (run via the runner's
  subprocess protocol);
- zero ranking difference: ID lists, order and bit-exact scores must be
  identical between python and rust lanes (P1-H parity proof);
- exact candidate/source identity recorded (git SHA, clean flag, script
  and module hashes);
- no material regression for small scopes where FFI may dominate.

Dedicated output files only — never the shared benchmark.log.

This is VECTOR-ONLY evidence (identical stored vectors, hash embedding
of fixed dimension). It is NOT selected-model end-to-end evidence; a
model lane is a separate script that instantiates the actual selected
model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.benchmarks.quantiles import percentile  # noqa: E402
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


def _run_lane(
    index: LocalVectorIndex, scope: Scope, query: str, reps: int
) -> dict[str, Any]:
    """Warm up once, then record RAW per-query samples + hit identity."""
    index.search(scope, query, limit=20)  # warm-up (excluded from samples)
    raw_ms: list[float] = []
    hit_ids: list[str] = []
    hit_scores: list[float] = []
    for _ in range(reps):
        t0 = time.perf_counter()
        hits = index.search(scope, query, limit=20)
        raw_ms.append((time.perf_counter() - t0) * 1000.0)
        if not hit_ids:  # first measured call defines identity (all equal)
            hit_ids = [h.memory_id for h in hits]
            hit_scores = [h.score for h in hits]
    ordered = sorted(raw_ms)
    diag = index.backend_diagnostics()
    return {
        "raw_ms": [round(v, 6) for v in raw_ms],
        "p50": round(percentile(ordered, 50.0), 4),
        "p95": round(percentile(ordered, 95.0), 4),
        "p99": round(percentile(ordered, 99.0), 4),
        "min": round(ordered[0], 4),
        "selected": diag["selected"],
        "native_version": diag["native_version"],
        "hit_ids": hit_ids,
        "hit_scores": hit_scores,
    }


def _git_identity() -> dict[str, str]:
    """Exact candidate/source identity (P1-H provenance binding)."""
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
    ap = argparse.ArgumentParser(description="native vs python e2e search gate")
    ap.add_argument("--n", type=int, default=10_000, help="rows in scope")
    ap.add_argument("--dim", type=int, default=384)
    ap.add_argument("--reps", type=int, default=15)
    ap.add_argument("--out", type=Path, required=True, help="dedicated JSON output")
    args = ap.parse_args()

    queries = [
        "the service processed requests",
        "system logged warnings",
        "site observation",
    ]
    results: dict[str, Any] = {
        "kind": "vector-only-e2e",
        "n": args.n,
        "dim": args.dim,
        "reps": args.reps,
        "queries": queries,
        "quantile_method": "linear-interpolation (type 7) over raw_ms",
        "source": _git_identity(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "lanes": {},
        "parity": {},
    }
    lanes: dict[str, dict[str, dict[str, Any]]] = {}
    for backend in ("python", "rust"):
        os.environ["SEVERIAN_VECTOR_BACKEND"] = backend
        lane: dict[str, dict[str, Any]] = {}
        for qi, query in enumerate(queries):
            path = Path(f"/tmp/rem-native-gate-{backend}-{qi}-{os.getpid()}.db")
            index = LocalVectorIndex(path, HashEmbedding(dimension=args.dim))
            scope = Scope(
                tenant_id="t", profile_id="p", collection_id="c",
                agent_id="a", session_id=f"s-{backend}-{qi}",
            )
            _seed_scope(index, scope, args.n, args.dim)
            lane[f"q{qi}"] = _run_lane(index, scope, query, args.reps)
            index.close()
            path.unlink(missing_ok=True)
        lanes[backend] = lane
    results["lanes"] = lanes

    # Parity: python vs rust ID lists, order and bit-exact scores.
    parity: dict[str, Any] = {}
    max_score_diff = 0.0
    for qi in range(len(queries)):
        py = lanes["python"][f"q{qi}"]
        rs = lanes["rust"][f"q{qi}"]
        parity[f"q{qi}"] = {
            "id_parity": py["hit_ids"] == rs["hit_ids"],
            "order_parity": py["hit_ids"] == rs["hit_ids"],
            "score_parity": py["hit_scores"] == rs["hit_scores"],
            "max_score_diff": max(
                (abs(a - b) for a, b in zip(py["hit_scores"], rs["hit_scores"], strict=False)),
                default=0.0,
            ),
        }
        max_score_diff = max(max_score_diff, parity[f"q{qi}"]["max_score_diff"])
    results["parity"] = parity
    results["parity_overall"] = {
        "id_parity": all(parity[f"q{qi}"]["id_parity"] for qi in range(len(queries))),
        "order_parity": all(parity[f"q{qi}"]["order_parity"] for qi in range(len(queries))),
        "score_parity": all(parity[f"q{qi}"]["score_parity"] for qi in range(len(queries))),
        "max_score_diff": max_score_diff,
    }

    # Relative improvement at p95 (python baseline vs rust).
    results["p95_improvement_overall"] = {
        f"q{i}": 1.0 - lanes["rust"][f"q{i}"]["p95"] / lanes["python"][f"q{i}"]["p95"]
        for i in range(len(queries))
    }
    args.out.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(json.dumps({
        k: results[k]
        for k in ("kind", "n", "dim", "source", "parity_overall",
                  "p95_improvement_overall")
    }, indent=2))


if __name__ == "__main__":
    main()
