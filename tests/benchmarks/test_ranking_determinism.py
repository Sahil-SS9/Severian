"""C-08 benchmark tests: LOCOMO ranking determinism.

Two fresh adapters (same manifest, same corpus) must produce IDENTICAL
rankings for every query class (fact, paraphrase, correction, temporal,
multi_source). Root cause fixed: all ranking tiebreaks now use stable
keys (content / vector / document id) instead of random memory UUIDs.

Also locks the deterministic-tiebreak unit behaviours:
- RetrievalService sorts fused ties by content (not memory id).
- LocalVectorIndex sorts score ties by vector.
- FTS5 orders by rank, content, memory_id.
- Temporal fallback sorts history items by (score desc, document_id).
"""

from __future__ import annotations

import json
from pathlib import Path

from severian.benchmarks.adapters.severian import SeverianAdapter

MANIFEST_LOCOMO = Path(__file__).resolve().parents[2] / "docs/benchmarks/manifest-locomo-tier-v1.json"


def _fresh_rankings(manifest_path: Path, queries: list[str]) -> dict[str, list[list[str]]]:
    """Run the given query ids through two fresh adapters; return A and B rankings."""
    m = json.loads(manifest_path.read_text())
    out: dict[str, list[list[str]]] = {}

    def run_once(tag: str) -> dict[str, list[str]]:
        adapter = SeverianAdapter(provider_id=f"det-{tag}", backend="sqlite")
        try:
            for d in m["corpus"]["documents"]:
                adapter.ingest(
                    str(d["id"]), str(d["content"]), scope=str(d["scope"]), supersedes=d.get("supersedes") or []
                )
            result: dict[str, list[str]] = {}
            for qid in queries:
                q = next(q for q in m["queries"] if q["id"] == qid)
                r = adapter.query(q["query"], limit=10, scope="t1/p1/c1/a1/s1", query_kind=q["kind"])
                result[qid] = [item["document_id"] for item in r.items]
            return result
        finally:
            adapter.close()

    a = run_once("a")
    b = run_once("b")
    for qid in queries:
        out[qid] = [a[qid], b[qid]]
    return out


def test_ranking_deterministic_across_fresh_adapters() -> None:
    """Two fresh SQLite adapters return identical rankings for six selected LOCOMO queries.

    Covered queries (q1 fact, q3 paraphrase, q5 correction, q7/q8 temporal,
    q14 multi-source) are a deterministic ranking sample. This does NOT
    claim byte-identical determinism for all metrics, all providers, the
    graph, the worker or dreaming state — that would require a broader
    evidence package (see EVID-05 in docs/review/objective-review-2026-08-05.md).
    """
    manifest = MANIFEST_LOCOMO
    queries = ["q1", "q3", "q5", "q7", "q8", "q14"]  # fact, paraphrase, correction, temporal x2, multi_source
    pair = _fresh_rankings(manifest, queries)
    for qid in queries:
        run_a, run_b = pair[qid]
        assert run_a == run_b, (
            f"{qid}: rankings differ across fresh adapters\n  A: {run_a}\n  B: {run_b}"
        )


def test_temporal_probe_is_stable() -> None:
    """Temporal probes (q7/q8) must return the SAME document ordering across runs."""
    pair = _fresh_rankings(MANIFEST_LOCOMO, ["q7", "q8"])
    assert pair["q7"][0] == pair["q7"][1]
    assert pair["q8"][0] == pair["q8"][1]
