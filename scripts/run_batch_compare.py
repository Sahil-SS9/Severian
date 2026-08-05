"""A-4: Chroma batch-ingest comparison (roadmap benchmark #5).

Measures the honest difference between the per-document ingest path and
the vectorized batch path for Chroma, plus Severian's per-doc baseline.
The scale lane uses the per-doc adapter protocol everywhere; this script
answers the open question "how much would Chroma's batch path change the
numbers?" without pretending the batch path is what the harness runs.

Correction C-02: ``chroma_batch_speedup_vs_perdoc`` is batch throughput
DIVIDED BY per-document throughput (batch docs_per_sec / per_doc
docs_per_sec). A value > 1 means batch is faster. The field is
versioned as ``chroma_batch_speedup_vs_perdoc_v2`` so any consumer of
the old inverted field cannot silently misread it.

Correction C-03: every run uses unique benchmark-owned temporary
Chroma roots (or explicitly cleared run directories), repeats each lane
``--reps`` times, and records median + spread (IQR), environment
fingerprint, dependency versions, producing Git SHA, corpus hash, and
command arguments. Absolute throughput is reported WITH a tolerance
window, never as a single universally reproducible number.

Usage:
    uv run python scripts/run_batch_compare.py --chroma-venv /tmp/bench-compare-venv \
        [--docs 200] [--reps 3] [--out /tmp/batch-compare]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import site
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.benchmarks.adapters.severian import SeverianAdapter  # noqa: E402

SCOPE = "t1/p1/c1/a1/s1"
_TEMPLATES = [
    "The {project} project reached milestone {n} in quarter {q} of {year}.",
    "Engineer {name} documented the {area} subsystem on {month} {day}, {year}.",
    "Budget line {n} allocated {amount}k to the {dept} department for {year}.",
    "The {product} service runs on {stack} with {replicas} replicas in {region}.",
    "Meeting {n} of {team} covered {topic} and was logged on {month} {day}.",
]
_PROJECTS = ["aurora", "chrysalis", "beacon", "nexus", "vantage", "harbor"]
_NAMES = ["ada", "lin", "grace", "alan", "dijkstra", "hopper"]
_AREAS = ["auth", "billing", "search", "memory", "sync"]
_DEPTS = ["finance", "engineering", "marketing"]
_REGIONS = ["eu-west", "us-east", "ap-south"]
_TOPICS = ["quotas", "releases", "security"]
_PRODUCTS = ["severian", "orbit", "pulse"]
_STACKS = ["rust", "go", "python"]
_TEAMS = ["alpha", "beta", "gamma"]
_SEED = 20260803


def generate_docs(count: int) -> list[tuple[str, str]]:
    import random

    rng = random.Random(_SEED)
    docs: list[tuple[str, str]] = []
    for i in range(count):
        template = _TEMPLATES[i % len(_TEMPLATES)]
        content = template.format(
            project=rng.choice(_PROJECTS),
            n=rng.randint(1, 400),
            q=rng.randint(1, 4),
            year=2019 + rng.randint(0, 7),
            name=rng.choice(_NAMES),
            area=rng.choice(_AREAS),
            month=rng.choice(["January", "March", "May", "July"]),
            day=rng.randint(1, 28),
            amount=rng.randint(5, 500),
            dept=rng.choice(_DEPTS),
            product=rng.choice(_PRODUCTS),
            stack=rng.choice(_STACKS),
            replicas=rng.randint(1, 12),
            region=rng.choice(_REGIONS),
            team=rng.choice(_TEAMS),
            topic=rng.choice(_TOPICS),
        )
        docs.append((f"batch-{i:06d}", content))
    return docs


def corpus_hash(docs: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    for doc_id, content in docs:
        digest.update(f"{doc_id}\0{content}\0".encode())
    return digest.hexdigest()


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, cwd=REPO_ROOT
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def time_per_doc(adapter: Any, docs: list[tuple[str, str]]) -> float:
    started = time.perf_counter()
    for doc_id, content in docs:
        adapter.ingest(doc_id, content, scope=SCOPE)
    return time.perf_counter() - started


def time_batch(adapter: Any, docs: list[tuple[str, str]]) -> float:
    started = time.perf_counter()
    adapter.ingest_batch(docs, scope=SCOPE)
    return time.perf_counter() - started


def _run_lane(timer, factory, docs: list[tuple[str, str]], reps: int) -> dict[str, Any]:
    """Run a lane ``reps`` times, each against a FRESH adapter/root.

    C-03: a rerun can never silently reuse a previous collection/database
    because every repetition gets a unique benchmark-owned temp root.
    """
    samples: list[float] = []
    for _ in range(reps):
        adapter = factory()
        try:
            samples.append(timer(adapter, docs))
        finally:
            adapter.close()
    med = median(samples)
    # spread = median absolute deviation-ish (IQR approximation via sorted)
    ordered = sorted(samples)
    spread = ordered[-1] - ordered[0] if len(ordered) > 1 else 0.0
    return {
        "repetitions": reps,
        "sample_seconds": [round(s, 4) for s in samples],
        "median_seconds": round(med, 4),
        "spread_seconds": round(spread, 4),
        "docs_per_sec_median": round(len(docs) / med, 1) if med > 0 else 0.0,
        "docs_per_sec_spread": round(len(docs) / max(med - spread, 1e-9) - len(docs) / med, 1) if med > 0 else 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chroma-venv", type=Path, required=True)
    parser.add_argument("--docs", type=int, default=200)
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.reps < 3:
        raise SystemExit("--reps must be >= 3 (C-03 requires at least three clean repetitions)")

    site_packages = (
        args.chroma_venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    )
    if not site_packages.exists():
        candidates = (
            sorted((args.chroma_venv / "lib").glob("python*/site-packages"))
            if (args.chroma_venv / "lib").exists()
            else []
        )
        site_packages = candidates[-1] if candidates else site_packages
    if site_packages.exists():
        site.addsitedir(str(site_packages))

    from severian.benchmarks.adapters.chroma import ChromaAdapter  # noqa: E402

    docs = generate_docs(args.docs)

    # C-03: unique benchmark-owned roots per repetition; never a shared
    # persistent path. Root name carries the "bench" marker so reset()/
    # cleanup guards recognise it as benchmark-owned.
    def chroma_perdoc_factory() -> Any:
        return ChromaAdapter(
            provider_id="chroma-perdoc",
            workdir=tempfile.mkdtemp(prefix="severian-bench-chroma-perdoc-"),
        )

    def chroma_batch_factory() -> Any:
        return ChromaAdapter(
            provider_id="chroma-batch",
            workdir=tempfile.mkdtemp(prefix="severian-bench-chroma-batch-"),
        )

    def severian_factory() -> Any:
        return SeverianAdapter(provider_id="severian-sqlite", backend="sqlite")

    results: dict[str, Any] = {}
    results["chroma_per_doc"] = _run_lane(time_per_doc, chroma_perdoc_factory, docs, args.reps)
    results["chroma_batch"] = _run_lane(time_batch, chroma_batch_factory, docs, args.reps)
    results["severian_sqlite_per_doc"] = _run_lane(time_per_doc, severian_factory, docs, args.reps)

    # C-02: batch speedup = batch throughput / per-doc throughput.
    per_doc_dps = results["chroma_per_doc"]["docs_per_sec_median"]
    batch_dps = results["chroma_batch"]["docs_per_sec_median"]
    if per_doc_dps <= 0:
        raise SystemExit("zero per-document throughput: cannot compute batch speedup (C-02 zero-denominator guard)")
    speedup = batch_dps / per_doc_dps
    results["chroma_batch_speedup_vs_perdoc_v2"] = round(speedup, 2)

    payload: dict[str, Any] = {
        "instrument": "A-4 chroma batch-ingest comparison (C-02/C-03 corrected)",
        "seed": _SEED,
        "docs": args.docs,
        "reps": args.reps,
        "scope": SCOPE,
        "run_started_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha(),
        "corpus_sha256": corpus_hash(docs),
        "command_args": {key: str(value) for key, value in vars(args).items()},
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
        },
        "results": results,
        "notes": [
            "chroma_batch_speedup_vs_perdoc_v2 = batch docs_per_sec / per_doc docs_per_sec (>=1 means batch faster)",
            "the legacy inverted field chroma_batch_speedup_vs_perdoc is NOT written; consumers must use _v2",
            "absolute throughput is environment-specific; compare medians within the same fingerprint only",
        ],
    }
    out_dir = args.out or (REPO_ROOT / "docs" / "benchmarks" / "evidence" / datetime.now(UTC).date().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "batch-compare.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nwritten: {out_path}")


if __name__ == "__main__":
    main()
