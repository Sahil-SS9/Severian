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


def _make_repo(
    tmp: str, size: int, corpus_mode: str = "dense"
) -> tuple[SQLiteRepository, Scope, list[Memory]]:
    root = Path(tmp)
    repo = SQLiteRepository(path=root / f"sev-{size}.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="scale"))
    scope = Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1"
    )
    memories: list[Memory] = []
    for i in range(size):
        if corpus_mode == "dense":
            # ~10% near-duplicates (same fact, reworded) to give dreaming
            # real work; the rest unique but SHARING a common template
            # ("fact number X about the scale corpus") — adversarial to
            # LSH: every doc lands in the same buckets, so the pre-filter
            # degenerates toward all-pairs (the honest worst case).
            if i % 10 == 0:
                content = f"the backup job runs every night at 02:00 utc (entry {i // 10})"
            else:
                content = f"fact number {i} about the scale corpus"
        else:
            # diverse: genuinely disjoint vocabularies per topic, so
            # unrelated pairs share ~0 tokens (Jaccard ~0.0-0.1) and LSH
            # buckets stay small. Near-duplicates (~10%) reuse the SAME
            # vocabulary, giving dreaming real work.
            topic_templates = {
                "network latency": ("the ping between nodes is", "ms"),
                "database index": ("the btree index depth reached", "levels"),
                "cache hit rate": ("the redis hit ratio was", "percent"),
                "auth token": ("the oauth token expired after", "minutes"),
                "api quota": ("the hourly api quota reset at", "utc"),
                "log rotation": ("the syslog rotation kept", "archives"),
                "backup window": ("the nightly backup finished in", "minutes"),
                "deploy region": ("the canary rollout covered", "zones"),
                "queue depth": ("the rabbitmq queue backlog hit", "messages"),
                "memory usage": ("the heap footprint grew to", "gigabytes"),
                "disk iops": ("the nvme volume sustained", "iops"),
                "tls handshake": ("the tls handshake completed in", "roundtrips"),
                "batch job": ("the spark batch processed", "partitions"),
                "replica lag": ("the postgres replica lag reached", "seconds"),
                "connection pool": ("the pgbouncer pool saturated at", "connections"),
                "rate limit": ("the gateway rate limit allowed", "requests"),
                "schema migration": ("the prisma migration applied", "steps"),
                "feature flag": ("the launchdarkly flag flipped for", "users"),
                "alert rule": ("the prometheus alert fired after", "thresholds"),
                "config drift": ("the terraform plan detected", "drifts"),
            }
            topics = list(topic_templates)
            topic = topics[i % len(topics)]
            phrase, unit = topic_templates[topic]
            if i % 10 == 0:
                content = f"{phrase} {i % 100} {unit} (entry {i // 10})"
            else:
                content = f"{phrase} {i % 50} {unit} observed at {i % 24:02d}:00"
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
    parser.add_argument(
        "--corpus-mode",
        choices=("dense", "diverse"),
        default="dense",
        help="dense = adversarial to LSH (shared template); diverse = real-world-ish",
    )
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
                "corpus_mode=dense: every doc shares a common template, so "
                "MinHash-LSH buckets collapse toward all-pairs — the "
                "adversarial worst case for the pre-filter (no win over "
                "brute force). corpus_mode=diverse: per-topic vocabularies, "
                "so LSH prunes the O(n^2) scan (the realistic win). "
                "Curation ALSO calls ConsolidationService._find_contradictions, "
                "so curation inherits the same scaling behaviour as "
                "consolidation. The scoring loop itself is O(n); the "
                "contradiction scan dominates. Not a throughput claim; the "
                "cost curve is the finding."
            ),
        }

    for size in buckets:
        with tempfile.TemporaryDirectory() as tmp:
            repo, scope, _ = _make_repo(tmp, size, args.corpus_mode)

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
