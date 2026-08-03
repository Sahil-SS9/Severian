"""A-4: Chroma batch-ingest comparison (roadmap benchmark #5).

Measures the honest difference between the per-document ingest path and
the vectorized batch path for Chroma, plus Severian's per-doc baseline.
The scale lane uses the per-doc adapter protocol everywhere; this script
answers the open question "how much would Chroma's batch path change the
numbers?" without pretending the batch path is what the harness runs.

Usage:
    uv run python scripts/run_batch_compare.py --chroma-venv /tmp/bench-compare-venv \
        [--docs 200] [--out /tmp/batch-compare]
"""
from __future__ import annotations

import argparse
import json
import site
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
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


def time_per_doc(adapter: Any, docs: list[tuple[str, str]]) -> float:
    started = time.perf_counter()
    for doc_id, content in docs:
        adapter.ingest(doc_id, content, scope=SCOPE)
    return time.perf_counter() - started


def time_batch(adapter: Any, docs: list[tuple[str, str]]) -> float:
    started = time.perf_counter()
    adapter.ingest_batch(docs, scope=SCOPE)
    return time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chroma-venv", type=Path, required=True)
    parser.add_argument("--docs", type=int, default=200)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

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

    results: dict[str, Any] = {}

    # Chroma per-doc (what the harness measures) vs batch (vendor-recommended).
    chroma_perdoc = ChromaAdapter(provider_id="chroma-perdoc", workdir="/tmp/batch-compare-chroma-perdoc")
    try:
        elapsed = time_per_doc(chroma_perdoc, docs)
        results["chroma_per_doc"] = {"seconds": round(elapsed, 3), "docs_per_sec": round(args.docs / elapsed, 1)}
    finally:
        chroma_perdoc.close()

    chroma_batch = ChromaAdapter(provider_id="chroma-batch", workdir="/tmp/batch-compare-chroma-batch")
    try:
        elapsed = time_batch(chroma_batch, docs)
        results["chroma_batch"] = {"seconds": round(elapsed, 3), "docs_per_sec": round(args.docs / elapsed, 1)}
    finally:
        chroma_batch.close()

    # Severian per-doc baseline (the harness path) for context.
    sev = SeverianAdapter(provider_id="severian-sqlite", backend="sqlite")
    try:
        elapsed = time_per_doc(sev, docs)
        results["severian_sqlite_per_doc"] = {
            "seconds": round(elapsed, 3),
            "docs_per_sec": round(args.docs / elapsed, 1),
        }
    finally:
        sev.close()

    speedup = (
        results["chroma_per_doc"]["docs_per_sec"] / results["chroma_batch"]["docs_per_sec"]
        if results["chroma_batch"]["docs_per_sec"]
        else 0.0
    )
    results["chroma_batch_speedup_vs_perdoc"] = round(speedup, 2)

    payload = {
        "instrument": "A-4 chroma batch-ingest comparison",
        "seed": _SEED,
        "docs": args.docs,
        "scope": SCOPE,
        "run_started_at": datetime.now(UTC).isoformat(),
        "results": results,
    }
    out_dir = args.out or (REPO_ROOT / "docs" / "benchmarks" / "evidence" / datetime.now(UTC).date().isoformat())
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "batch-compare.json"
    out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\nwritten: {out_path}")


if __name__ == "__main__":
    main()
