"""Scale lane: throughput and latency at 1k / 10k / 100k documents.

Runs each live adapter over a deterministic, seeded synthetic corpus and
reports ingest throughput (docs/sec) and query latency (p50/p95/p99) per
bucket. Evidence lands in docs/benchmarks/evidence/<date>/scale-*.json.

Usage:
    uv run python scripts/run_scale_benchmark.py \
        --chroma-venv /tmp/bench-compare-venv \
        --pg-dsn "postgresql://severian:severian@127.0.0.1:54329/severian" \
        [--max-bucket 10000] [--out /tmp/scale]
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.benchmarks.adapters.baseline import LexicalBaselineAdapter  # noqa: E402
from severian.benchmarks.adapters.severian import SeverianAdapter  # noqa: E402

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from bench_provenance import collect_provenance, corpus_hash  # noqa: E402

BUCKETS = (1_000, 10_000, 100_000)
QUERY_REPETITIONS = 3  # hot-query repetitions for p50/p95/p99 (quality rule 4)
QUERY_SET = [
    "What is the current office address?",
    "Who founded the company?",
    "What language does the core service use now?",
    "What is the private launch date of Project Chrysalis?",
    "What was the old office address?",
]
QUERY_SCOPE = "t1/p1/c1/a1/s1"

# 100k warmup budget: the 50-cap derive loop is super-linear (~2000
# passes, each re-scanning the scope). Bounded so the lane records an
# honest PARTIAL warmup (warmed_memories + warmup_complete=false)
# instead of a fake full-warm number or a multi-hundred-hour wait.
WARMUP_BUDGET_100K = 900.0  # seconds (15 min) per backend

_TEMPLATES = [
    "The {project} project reached milestone {n} in quarter {q} of {year}.",
    "Engineer {name} documented the {area} subsystem on {month} {day}, {year}.",
    "Budget line {n} allocated {amount}k to the {dept} department for {year}.",
    "The {product} service runs on {stack} with {replicas} replicas in {region}.",
    "Meeting {n} of {team} covered {topic} and was logged on {month} {day}.",
    "Incident {n} in {area} was resolved by {name} with a postmortem on {date}.",
    "The {tool} toolchain was upgraded to version {version} across {region}.",
    "KPI {kpi} for {dept} tracked {value} percent during {year}.",
    "Feature {feature} shipped behind a flag in release {release} of {product}.",
    "Team {team} adopted {practice} after reviewing the {year} retrospective.",
]

_NAMES = ["ada", "lin", "grace", "alan", "dijkstra", "hopper", "lovelace", "turing"]
_PROJECTS = ["aurora", "chrysalis", "beacon", "nexus", "vantage", "harbor"]
_AREAS = ["auth", "billing", "search", "memory", "sync", "analytics", "gateway"]
_DEPTS = ["finance", "engineering", "marketing", "sales", "support"]
_REGIONS = ["eu-west", "us-east", "ap-south", "eu-central"]
_TOPICS = ["quotas", "releases", "security", "roadmap", "costs"]
_PRODUCTS = ["severian", "orbit", "pulse", "keystone"]
_STACKS = ["rust", "go", "python", "kotlin"]
_TOOLS = ["cargo", "bazel", "make", "maven"]
_PRACTICES = ["continuous delivery", "chaos testing", "feature flags", "code review"]
_FEATURES = ["sync-v2", "search-lite", "audit-log", "rbac"]

_SEED = 20260802


def _seeded(rng: Any, template: str, index: int) -> str:
    return template.format(
        project=rng.choice(_PROJECTS),
        n=rng.randint(1, 400),
        q=rng.randint(1, 4),
        year=2019 + rng.randint(0, 7),
        name=rng.choice(_NAMES),
        area=rng.choice(_AREAS),
        month=rng.choice(["January", "March", "May", "July", "September", "November"]),
        day=rng.randint(1, 28),
        amount=rng.randint(5, 500),
        dept=rng.choice(_DEPTS),
        product=rng.choice(_PRODUCTS),
        stack=rng.choice(_STACKS),
        replicas=rng.randint(1, 12),
        region=rng.choice(_REGIONS),
        team=rng.choice(_PROJECTS),
        topic=rng.choice(_TOPICS),
        date=f"{2019 + rng.randint(0, 7)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        tool=rng.choice(_TOOLS),
        version=f"v{rng.randint(1, 12)}.{rng.randint(0, 9)}",
        kpi=rng.choice(["uptime", "latency", "saturation", "throughput"]),
        value=rng.randint(1, 99),
        feature=rng.choice(_FEATURES),
        release=rng.randint(1, 40),
        practice=rng.choice(_PRACTICES),
    )


def generate_corpus(count: int) -> list[tuple[str, str]]:
    rng = __import__("random").Random(_SEED)
    docs: list[tuple[str, str]] = []
    for i in range(count):
        template = _TEMPLATES[i % len(_TEMPLATES)]
        docs.append((f"scale-{i:06d}", _seeded(rng, template, i)))
    return docs


@dataclass
class LaneResult:
    provider_id: str
    bucket: int
    ingest_seconds: float
    docs_per_sec: float
    query_latencies_ms: list[float] = field(default_factory=list)
    cold_query_ms: float = 0.0
    warmed_memories: int = 0
    warmup_complete: bool = False

    @property
    def p50(self) -> float:
        return statistics.median(self.query_latencies_ms) if self.query_latencies_ms else 0.0

    @property
    def p95(self) -> float:
        if not self.query_latencies_ms:
            return 0.0
        idx = min(len(self.query_latencies_ms) - 1, int(0.95 * len(self.query_latencies_ms)))
        return sorted(self.query_latencies_ms)[idx]

    @property
    def p99(self) -> float:
        if not self.query_latencies_ms:
            return 0.0
        idx = min(len(self.query_latencies_ms) - 1, int(0.99 * len(self.query_latencies_ms)))
        return sorted(self.query_latencies_ms)[idx]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "bucket": self.bucket,
            "ingest_seconds": round(self.ingest_seconds, 3),
            "docs_per_sec": round(self.docs_per_sec, 1),
            "cold_first_query_ms": round(self.cold_query_ms, 2),
            "warmed_memories": self.warmed_memories,
            "warmup_complete": self.warmup_complete,
            "query_p50_ms": round(self.p50, 2),
            "query_p95_ms": round(self.p95, 2),
            "query_p99_ms": round(self.p99, 2),
            "queries_run": len(self.query_latencies_ms),
        }


def _make_adapter(pid: str, pg_dsn: str | None, chroma_venv: Path | None) -> Any:
    if pid == "severian-sqlite":
        return SeverianAdapter(provider_id=pid, backend="sqlite")
    if pid == "severian-postgres":
        if not pg_dsn:
            raise SystemExit("severian-postgres lane requires --pg-dsn")
        # Scratch benchmark DB: reset per lane so each bucket measures
        # exactly its own corpus, not accumulated earlier buckets.
        os.environ["SEVERIAN_PG_ALLOW_RESET"] = "1"
        return SeverianAdapter(provider_id=pid, backend="postgresql", dsn=pg_dsn, reset_db=True)
    if pid == "baseline-lexical":
        return LexicalBaselineAdapter(provider_id=pid)
    if pid == "chroma":
        if chroma_venv is None:
            raise SystemExit("chroma lane requires --chroma-venv")
        import site

        site_packages = (
            chroma_venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
        )
        if not site_packages.exists():
            candidates = (
                sorted((chroma_venv / "lib").glob("python*/site-packages"))
                if (chroma_venv / "lib").exists()
                else []
            )
            site_packages = candidates[-1] if candidates else site_packages
        if site_packages.exists():
            site.addsitedir(str(site_packages))
        from severian.benchmarks.adapters.chroma import ChromaAdapter

        return ChromaAdapter(provider_id=pid, workdir="/tmp/bench-scale-chroma")
    raise SystemExit(f"unknown lane {pid}")


def run_lane(
    pid: str,
    bucket: int,
    docs: list[tuple[str, str]],
    pg_dsn: str | None,
    chroma_venv: Path | None,
) -> LaneResult:
    adapter = _make_adapter(pid, pg_dsn, chroma_venv)
    result = LaneResult(provider_id=pid, bucket=bucket, ingest_seconds=0.0, docs_per_sec=0.0)
    try:
        t0 = time.perf_counter()
        for doc_id, content in docs:
            adapter.ingest(doc_id, content, scope=QUERY_SCOPE)
        elapsed = time.perf_counter() - t0
        result.ingest_seconds = elapsed
        result.docs_per_sec = bucket / elapsed if elapsed > 0 else 0.0

        # Cold path: first query after writes pays full derive+reindex.
        # derive-on-read is O(observations) per cold query, so the cost
        # is measured at every bucket (establishes the curve). It must
        # run at 100k too: derive=False hot queries read ONLY the
        # projection, which is empty until a derive pass runs. Skipping
        # the cold query at 100k silently made the hot loop measure an
        # EMPTY index (~0.1ms, 0 hits) — the bogus "1500x speedup"
        # caught by the monotonicity report. Cost at 100k is ~25 min
        # per severian lane; that is the honest price of a real number.
        # Steady-state queries then run on the hot path (derive=False),
        # which is what production serves after the worker derives.
        # Only the Severian adapter has a derive toggle; competitors
        # are measured on their natural path.
        #
        # NOTE (zero-debt, 2026-08-08): one search(derive=True) call is
        # capped at 50 NEW memories (derive_max_observations=50), so a
        # single cold query does NOT warm a 10k/100k index — hot timing
        # would measure a ~50-row index instead of the real bucket
        # (empty-index trap in disguise). The lane warms via
        # adapter.warmup() and records the derived count so the evidence
        # proves how much of the bucket was actually indexed.
        #
        # Warmup budget: the 50-cap loop is super-linear (~200 passes at
        # 10k, ~2000 at 100k, each pass re-scanning the whole scope).
        # 1k/10k warm to FULL stability; 100k is bounded (WARMUP_BUDGET_100K)
        # and recorded as a PARTIAL warmup — an honest partial number
        # beats a fake full one or a 750h wait (measured live 2026-08-09:
        # 10k full warmup exceeded 60 min).
        if pid.startswith("severian"):
            t1 = time.perf_counter()
            max_warm = None if bucket <= 10_000 else WARMUP_BUDGET_100K
            result.warmed_memories = adapter.warmup(
                scope=QUERY_SCOPE, max_seconds=max_warm
            )
            result.cold_query_ms = (time.perf_counter() - t1) * 1000.0
            result.warmup_complete = result.warmed_memories >= bucket

        for query in QUERY_SET:
            t1 = time.perf_counter()
            if pid.startswith("severian"):
                adapter.query(query, limit=5, scope=QUERY_SCOPE, derive=False)
            else:
                adapter.query(query, limit=5, scope=QUERY_SCOPE)
            result.query_latencies_ms.append((time.perf_counter() - t1) * 1000.0)
        # Repeated hot queries make p50/p95/p99 statistically meaningful
        # (quality rule 4: repetitions are recorded, not implied).
        for _ in range(max(1, QUERY_REPETITIONS - 1)):
            for query in QUERY_SET:
                t1 = time.perf_counter()
                if pid.startswith("severian"):
                    adapter.query(query, limit=5, scope=QUERY_SCOPE, derive=False)
                else:
                    adapter.query(query, limit=5, scope=QUERY_SCOPE)
                result.query_latencies_ms.append((time.perf_counter() - t1) * 1000.0)
    finally:
        adapter.close()
    return result


def monotonicity_report(results: list[LaneResult]) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for pid in sorted({r.provider_id for r in results}):
        lane = sorted((r for r in results if r.provider_id == pid), key=lambda r: r.bucket)
        if len(lane) < 2:
            report[pid] = {"assessed": False, "reason": "fewer than two buckets measured"}
            continue
        p95s = [r.p95 for r in lane]
        # pairwise yields adjacent (a, b) pairs: for a 3-bucket lane it
        # gives (b1,b2), (b2,b3) and for 0/1-item lists it yields nothing
        # (all() -> True). Never use zip(..., strict=True) here: p95s[1:]
        # is intentionally one element shorter than p95s, so strict mode
        # would raise ValueError on every non-empty lane.
        monotonic = all(b >= a for a, b in itertools.pairwise(p95s))
        throughput = [r.docs_per_sec for r in lane]
        report[pid] = {
            "assessed": True,
            "p95_monotonic_non_decreasing": monotonic,
            "p95_series": [round(v, 2) for v in p95s],
            "throughput_series": [round(v, 1) for v in throughput],
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chroma-venv", type=Path)
    parser.add_argument("--pg-dsn", type=str)
    parser.add_argument("--max-bucket", type=int, default=100_000)
    parser.add_argument("--min-bucket", type=int, default=1_000)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    buckets = [b for b in BUCKETS if args.min_bucket <= b <= args.max_bucket]
    if not buckets:
        raise SystemExit(f"no buckets in [{args.min_bucket}, {args.max_bucket}]")
    lanes = ["severian-sqlite"]
    if args.pg_dsn:
        lanes.append("severian-postgres")
    if args.chroma_venv:
        lanes.append("chroma")
    lanes.append("baseline-lexical")

    all_results: list[LaneResult] = []
    skipped: list[dict[str, str]] = []
    run_started_at = datetime.now(UTC).isoformat()
    out_dir = args.out or (REPO_ROOT / "docs" / "benchmarks" / "evidence" / datetime.now(UTC).date().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    scale_path = out_dir / "scale.json"

    def write_evidence() -> None:
        payload = {
            "instrument": "severian scale lane",
            "seed": _SEED,
            "buckets": buckets,
            "lanes": lanes,
            "query_set": QUERY_SET,
            "results": [r.to_dict() for r in all_results],
            "skipped_lanes": skipped,
            "monotonicity": monotonicity_report(all_results),
            "run_started_at": run_started_at,
            "run_state": "complete" if len(all_results) + len(skipped) >= len(buckets) * len(lanes) else "partial",
            "repro": collect_provenance(
                REPO_ROOT,
                sys.argv,
                seed=_SEED,
                corpus_fingerprint=lambda: corpus_hash(generate_corpus, max(buckets)),
            ),
            "repetitions": QUERY_REPETITIONS,
        }
        scale_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    # Evidence is written after EVERY lane, not just at the end: a run
    # killed by timeout/oom still preserves every completed lane instead
    # of discarding ~45 minutes of work (the 2026-08-03 exit-124 loss).
    # run_state distinguishes a partial dump from a full one.
    for bucket in buckets:
        docs = generate_corpus(bucket)
        for pid in lanes:
            # Chroma's per-document upsert path costs ~0.31 s/doc (the
            # adapter protocol is one write per call), so the 100k bucket
            # would take ~9 hours. That per-doc write cost is itself a
            # competitive finding, but it is not worth a 9-hour lane;
            # report 1k/10k for chroma and skip 100k with the reason.
            if pid == "chroma" and bucket >= 100_000:
                skipped.append({
                    "provider_id": pid,
                    "bucket": str(bucket),
                    "reason": "per-doc ingest ~0.31s/doc would take ~9h",
                })
                write_evidence()
                continue
            print(f"  [{bucket:>6} docs] {pid} ...", flush=True)
            try:
                result = run_lane(pid, bucket, docs, args.pg_dsn, args.chroma_venv)
                all_results.append(result)
                print(f"    ingest {result.docs_per_sec:,.0f} docs/s  p50 {result.p50:.1f}ms p95 {result.p95:.1f}ms")
            except Exception as exc:  # noqa: BLE001 - lane isolation
                print(f"    FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
            write_evidence()

    write_evidence()
    print(f"\nscale evidence written to: {scale_path}")
    print(f"  scale.json sha256: {hashlib.sha256(scale_path.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
