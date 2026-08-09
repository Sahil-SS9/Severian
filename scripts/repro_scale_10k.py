"""Isolated repro: 10k scale-corpus ingest + warmup on CURRENT code.

A3 evidence: 45 min in the 10k sqlite lane (ingest ~5.4 docs/s, then
warmup frozen at 7000/10000 memories at 100% CPU, zero writes). This
script times ingest and warmup separately so the bottleneck is
attributable, not guessed.
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_scale_benchmark import generate_corpus  # noqa: E402
from severian.benchmarks.adapters.severian import SeverianAdapter  # noqa: E402

QUERY_SCOPE = "t/bench/c/bench-agent/bench-session"


def main() -> None:
    bucket = 10_000
    docs = generate_corpus(bucket)
    print(f"corpus: {len(docs)} docs, unique contents: {len(set(c for _i, c in docs))}")

    workdir = tempfile.mkdtemp(prefix="severian-repro-")
    adapter = SeverianAdapter(
        provider_id="severian-sqlite", backend="sqlite", workdir=workdir,
    )

    t0 = time.perf_counter()
    for doc_id, content in docs:
        adapter.ingest(doc_id, content, scope=QUERY_SCOPE)
    ingest_s = time.perf_counter() - t0
    print(f"ingest: {bucket} docs in {ingest_s:.1f}s = {bucket / ingest_s:.1f} docs/s")

    t1 = time.perf_counter()
    warmed = adapter.warmup(scope=QUERY_SCOPE, max_seconds=600)
    warm_s = time.perf_counter() - t1
    print(f"warmup: {warmed} memories in {warm_s:.1f}s ({warmed / warm_s:.0f} mem/s)")
    print(f"warmup_complete: {warmed >= bucket}")

    # hot queries
    t2 = time.perf_counter()
    for q in ["backup job completed", "nightly report", "database migration",
              "memory retention policy", "api rate limit"]:
        adapter.query(q, limit=5, scope=QUERY_SCOPE, derive=False)
    q_s = (time.perf_counter() - t2) * 1000
    print(f"5 hot queries: {q_s:.0f}ms total ({q_s / 5:.0f}ms each)")

    adapter.close()
    result = {"ingest_docs_per_sec": round(bucket / ingest_s, 1), "warmed": warmed,
              "warmup_complete": warmed >= bucket}
    Path("/tmp/scale-repro-10k.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
