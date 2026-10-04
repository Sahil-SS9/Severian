"""Time search per Step 2 sub-change on a synthetic conversation store (p50/p95 ms).

Usage: python scripts/bench_search_tuning.py [turns]   (default 400 turns over 20 conversations)
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

from severian.application.retrieval import RetrievalService, SearchTuning
from severian.application.worker import DerivationWorker
from severian.composition import build_bundle
from severian.domain.models import Scope

SCOPE = Scope(tenant_id="bench", profile_id="p", collection_id="c", agent_id="a", session_id="s")
CASES = {
    "default": SearchTuning(),
    "weights 2:1": SearchTuning(lexical_weight=2.0),
    "k=20": SearchTuning(fusion_k=20),
    "neighbours=1": SearchTuning(neighbours=1),
    "neighbours=2": SearchTuning(neighbours=2),
}


def main(turns: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bundle = build_bundle(database=Path(tmp) / "b.db", fts=Path(tmp) / "b.fts", vectors=Path(tmp) / "b.vec")
        try:
            for index in range(turns):
                content = (
                    f"Conversation {index // 20} turn {index}: topic{index % 37} item{index % 11} "
                    f"note about project{index % 5}"
                )
                bundle.service.ingest_observation(SCOPE, content=content, source={"session_id": f"conv-{index // 20}"})
            DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
            queries = [f"topic{n % 37} item{n % 11}" for n in range(60)]
            print(f"{turns} turns, {len(queries)} queries")
            for name, tuning in CASES.items():
                service = RetrievalService(bundle.repository, bundle.lexical, bundle.vector, tuning)
                timings = []
                for query in queries:
                    start = time.perf_counter()
                    service.search(SCOPE, query, limit=10)
                    timings.append((time.perf_counter() - start) * 1000)
                timings.sort()
                p95 = timings[int(len(timings) * 0.95) - 1]
                print(f"{name:14s} p50 {statistics.median(timings):8.2f} ms   p95 {p95:8.2f} ms")
        finally:
            bundle.close()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 400)
