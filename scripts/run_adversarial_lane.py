"""Run the adversarial-27 manifest on severian-sqlite (hash) and baseline."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path("/home/kensei/repos/Severian")
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.benchmarks.adapters import LexicalBaselineAdapter, SeverianAdapter  # noqa: E402
from severian.benchmarks.manifest import load_manifest  # noqa: E402
from severian.benchmarks.runner import BenchmarkRunner  # noqa: E402

manifest = load_manifest(REPO_ROOT / "docs/benchmarks/manifest-adversarial-v1.json")
runner = BenchmarkRunner(
    manifest=manifest,
    allow_missing=True,
    adapters={
        "severian-sqlite": SeverianAdapter(provider_id="severian-sqlite", backend="sqlite"),
        "baseline-lexical": LexicalBaselineAdapter(provider_id="baseline-lexical"),
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

out = {
    "manifest_id": result.manifest_id,
    "gate_passed": result.gate_passed,
    "gate_failures": result.gate_failures,
    "results": {
        pid: {
            "recall_at_10": pr.recall_at_10,
            "mrr": pr.mrr,
            "ndcg_at_10": pr.ndcg_at_10,
            "citation_precision": pr.citation_precision,
            "stale_fact_rate": pr.stale_fact_rate,
            "temporal_accuracy": pr.temporal_accuracy,
            "cross_scope_leakage": pr.cross_scope_leakage,
            "unsupported_answer_rate": pr.unsupported_answer_rate,
        }
        for pid, pr in result.results.items()
    },
}
with open("/tmp/adversarial-hash-result.json", "w") as f:
    json.dump(out, f, indent=2)
