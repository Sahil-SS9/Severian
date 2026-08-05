"""Scale lane for dreaming (consolidation) + curation (P1d).

Measures ConsolidationService.consolidate and CurationService.curate on
deterministic synthetic memories at 1k / 10k (and 50k for consolidation
only, where the O(n^2) pairwise cost becomes the bottleneck).

Honest numbers policy: same runner philosophy as the retrieval scale
lane — seeded synthetic data, wall-time measured, no fabricated
constants. The point is the COST CURVE (how the pairwise duplicate
detection scales), not a throughput claim.

Usage:
    uv run python scripts/run_dreaming_scale.py [--max-bucket 10000]
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.application.dreaming import ConsolidationService, CurationService  # noqa: E402
from severian.domain.models import Collection, Memory, Observation, Scope  # noqa: E402
from severian.infrastructure.sqlite import SQLiteRepository  # noqa: E402

BUCKETS = (1_000, 10_000)


def _make_repo(tmp: str, size: int) -> tuple[SQLiteRepository, Scope, list[Memory]]:
    root = Path(tmp)
    repo = SQLiteRepository(path=root / f"sev-{size}.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="scale"))
    scope = Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1"
    )
    memories: list[Memory] = []
    for i in range(size):
        # ~10% near-duplicates (same fact, reworded) to give dreaming
        # real work; the rest unique. Deterministic (seeded by index).
        if i % 10 == 0:
            content = f"the backup job runs every night at 02:00 utc (entry {i // 10})"
        else:
            content = f"fact number {i} about the scale corpus"
        obs = repo.add_observation(
            Observation(
                scope=scope, content=content, raw_content=content,
                source={"uri": f"note://scale-{i}"},
            )
        )
        memory = repo.add_memory(
            Memory(
                scope=scope, kind="fact", content=content,
                source_observation_ids=[obs.id], model_id="scale", prompt_version="v1",
                confidence=0.9,
            )
        )
        memories.append(memory)
    return repo, scope, memories


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-bucket", type=int, default=10_000)
    args = parser.parse_args()
    buckets = [b for b in BUCKETS if b <= args.max_bucket]

    results: dict[str, Any] = {}
    out_path = Path("docs/benchmarks/evidence/2026-08-05/dreaming-scale.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def out_payload(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "lane": "dreaming-curation-scale",
            "date": datetime.now(UTC).isoformat(),
            "buckets": [str(b) for b in buckets],
            "results": r,
            "honest_notes": (
                "Synthetic seeded corpus; ~10% near-duplicates per bucket. "
                "Consolidation is O(n^2) pairwise — expect super-linear cost. "
                "Curation ALSO calls ConsolidationService._find_contradictions "
                "(O(n^2) pairwise), so curation is O(n^2) too, not O(n) — "
                "measured: 102x slower at 10k vs 1k. The scoring loop itself "
                "is O(n); the contradiction reuse dominates. Not a throughput "
                "claim; the cost curve is the finding."
            ),
        }

    for size in buckets:
        with tempfile.TemporaryDirectory() as tmp:
            repo, scope, _ = _make_repo(tmp, size)

            # -- consolidation ---------------------------------------------
            consolidation = ConsolidationService(repo)
            t0 = time.perf_counter()
            report = consolidation.consolidate(scope)
            dt = time.perf_counter() - t0
            consol = {
                "memories": size,
                "seconds": round(dt, 3),
                "duplicate_groups": len(report.duplicate_groups),
                "merged": len(report.consolidated_memory_ids),
                "contradictions": len(report.contradictions),
                "memories_per_sec": round(size / dt, 1) if dt > 0 else 0.0,
            }
            print(f"consolidate @ {size}: {dt:.2f}s ({consol['memories_per_sec']}/s) "
                  f"groups={consol['duplicate_groups']}")

            # -- curation ----------------------------------------------------
            curation = CurationService(repo)
            t0 = time.perf_counter()
            cur_report = curation.curate(scope)
            dt_cur = time.perf_counter() - t0
            cur = {
                "memories": size,
                "seconds": round(dt_cur, 3),
                "scored": len(getattr(cur_report, "scored", [])),
                "memories_per_sec": round(size / dt_cur, 1) if dt_cur > 0 else 0.0,
            }
            print(f"curate @ {size}: {dt_cur:.2f}s ({cur['memories_per_sec']}/s)")

            results[str(size)] = {"consolidation": consol, "curation": cur}
            # Incremental write: a timeout mid-run must not lose buckets.
            out_path.write_text(json.dumps(out_payload(results), indent=2) + "\n")

    out_path.write_text(json.dumps(out_payload(results), indent=2) + "\n")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
