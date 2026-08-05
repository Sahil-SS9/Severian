"""Run adversarial-27 with Granite R2 embeddings (semantic zone test)."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path("/home/kensei/repos/Severian")
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.benchmarks.adapters import SeverianAdapter  # noqa: E402
from severian.benchmarks.manifest import load_manifest  # noqa: E402
from severian.benchmarks.runner import BenchmarkRunner  # noqa: E402
from severian.infrastructure.granite_embedding import GraniteR2Embedding  # noqa: E402

manifest = load_manifest(REPO_ROOT / "docs/benchmarks/manifest-adversarial-v1.json")
embedding = GraniteR2Embedding().or_hash()
runner = BenchmarkRunner(
    manifest=manifest,
    allow_missing=True,
    adapters={
        "severian-sqlite": SeverianAdapter(
            provider_id="severian-sqlite",
            backend="sqlite",
            embedding=embedding,
        ),
    },
)
result = runner.run()
print("gate_passed:", result.gate_passed)
print("gate_failures:", result.gate_failures)

with open(REPO_ROOT / "docs/benchmarks/manifest-adversarial-v1.json") as f:
    m = json.load(f)
expected = {q["id"]: [e["document_id"] for e in q["expected_evidence"]] for q in m["queries"]}

for pid, pr in result.results.items():
    print(
        f"{pid}: recall={pr.recall_at_10:.3f} mrr={pr.mrr:.3f} ndcg={pr.ndcg_at_10:.3f} "
        f"cit_prec={pr.citation_precision:.3f} stale={pr.stale_fact_rate:.3f} "
        f"temporal={pr.temporal_accuracy:.3f} leak={pr.cross_scope_leakage:.3f} "
        f"unsup={pr.unsupported_answer_rate:.3f}"
    )
    for item in pr.raw:
        qid = item["query_id"]
        exp = expected.get(qid, [])
        if not exp:
            if item["returned_ids"]:
                print(f"  UNSUP-FAIL {qid}: expected abstention, returned {item['returned_ids'][:3]}")
            continue
        n = len(exp)
        top = item["returned_ids"][:n]
        prec = len(set(exp) & set(top)) / n
        if prec < 1.0:
            print(f"  FAIL {qid}: expected {exp}, top{n}={top}")
