"""Generate the adversarial-27 manifest (A-3 option 3, 2026-08-05).

Extends the manifest-v1 corpus with adversarial cases that stress the
retrieval platform beyond the clean synthetic gate:

- typos / punctuation noise in queries (hash + BM25 both suffer)
- paraphrase distance (semantic embedding advantage zone)
- contradiction pairs (same subject, opposite claim — only one active)
- decoy documents (high lexical overlap, wrong answer)
- negative queries (answerable=False — abstention floor must fire)
- cross-scope decoys (documents in a different scope that lexically match)

Same manifest schema, same runner, same gate thresholds. Honest label:
still synthetic, but adversarially harder than manifest-v1.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path("docs/benchmarks/manifest-adversarial-v1.json")

DOCS: list[dict[str, object]] = [
    # -- reuse a few manifest-v1 facts as anchor docs ---------------------
    {"id": "a01", "content": "Grace Hopper invented COBOL in 1959.", "scope": "t1/p1/c1/a1/s1"},
    {"id": "a02", "content": "The current role of the project lead is research scientist.", "scope": "t1/p1/c1/a1/s1"},
    {
        "id": "a03",
        "content": "The old role of the project lead was software engineer.",
        "scope": "t1/p1/c1/a1/s1",
        "is_stale": True,
        "supersedes": [],
    },
    # a05 must be ingested BEFORE a04 (its superseder) so the correction
    # path can tombstone it; corpus order matters (superseded first).
    {
        "id": "a05",
        "content": "The old password manager stored secrets in plain text.",
        "scope": "t1/p1/c1/a1/s1",
        "is_stale": True,
        "supersedes": [],
    },
    {
        "id": "a04",
        "content": "The current password manager stores secrets using scrypt hashing.",
        "scope": "t1/p1/c1/a1/s1",
        "supersedes": ["a05"],
    },
    # -- typo / punctuation noise ------------------------------------------
    {
        "id": "a06",
        "content": "The deploy pipeline runs tests, lints, and builds on every push.",
        "scope": "t1/p1/c1/a1/s1",
    },
    {"id": "a07", "content": "The database uses WAL mode for crash-safe concurrent writes.", "scope": "t1/p1/c1/a1/s1"},
    # -- paraphrase distance (semantic zone) --------------------------------
    {
        "id": "a08",
        "content": "Every incoming request is authenticated before it reaches the handler.",
        "scope": "t1/p1/c1/a1/s1",
    },
    {
        "id": "a09",
        "content": "Requests are checked for valid credentials at the entry gate.",
        "scope": "t1/p1/c1/a1/s1",
    },
    # -- contradiction pairs (only one active) ------------------------------
    # Order matters: superseded docs first (a10 < a11 < a12).
    {"id": "a10", "content": "The service is deployed to the us-east-1 region.", "scope": "t1/p1/c1/a1/s1"},
    {
        "id": "a11",
        "content": "The service is deployed to the eu-west-1 region.",
        "scope": "t1/p1/c1/a1/s1",
        "is_stale": True,
        "supersedes": ["a10"],
    },
    {
        "id": "a12",
        "content": "The service is deployed to the us-east-1 region (final decision).",
        "scope": "t1/p1/c1/a1/s1",
        "supersedes": ["a11"],
    },
    # -- decoys (high lexical overlap, wrong answer) -------------------------
    {"id": "a13", "content": "The revenue target for Q3 is twelve million dollars.", "scope": "t1/p1/c1/a1/s1"},
    {"id": "a14", "content": "The revenue target for Q4 is fifteen million dollars.", "scope": "t1/p1/c1/a1/s1"},
    {"id": "a15", "content": "The headcount target for Q3 is twelve new engineers.", "scope": "t1/p1/c1/a1/s1"},
    # -- cross-scope decoys --------------------------------------------------
    {"id": "a16", "content": "The private launch date of Project Chrysalis is 2026-09-01.", "scope": "t2/p1/c1/a1/s1"},
    {"id": "a17", "content": "Project Chrysalis launch date is 2026-12-01.", "scope": "t1/p1/c1/a1/s1"},
    # -- multi-source ---------------------------------------------------------
    {
        "id": "a18",
        "content": "The signing ceremony for the Acme contract happens on Friday.",
        "scope": "t1/p1/c1/a1/s1",
    },
    {"id": "a19", "content": "The Acme contract was signed at the London office.", "scope": "t1/p1/c1/a1/s1"},
    # -- long-tail vocabulary --------------------------------------------------
    {
        "id": "a20",
        "content": "The hyperparameter sweep uses bayesian optimisation on the validation loss.",
        "scope": "t1/p1/c1/a1/s1",
    },
    {"id": "a21", "content": "The quantisation scheme is fp8 with a 128-token group size.", "scope": "t1/p1/c1/a1/s1"},
    # -- near-duplicate (dreaming target) -------------------------------------
    # a22 is the canonical phrasing; a23 (paraphrase) superseded by it.
    {
        "id": "a23",
        "content": "The backup job runs nightly at two in the morning UTC.",
        "scope": "t1/p1/c1/a1/s1",
        "is_stale": True,
        "supersedes": [],
    },
    {
        "id": "a22",
        "content": "The backup job runs every night at 02:00 UTC.",
        "scope": "t1/p1/c1/a1/s1",
        "supersedes": ["a23"],
    },
    # -- negative / abstention targets ----------------------------------------
    {"id": "a24", "content": "The incident review covered the payment gateway outage.", "scope": "t1/p1/c1/a1/s1"},
    {"id": "a25", "content": "The monitoring alert fired for the database replica lag.", "scope": "t1/p1/c1/a1/s1"},
    # -- temporal past/present pair -------------------------------------------
    # a26 (old) must precede a27 (current) so the correction path works.
    {
        "id": "a26",
        "content": "The old API returned XML payloads.",
        "scope": "t1/p1/c1/a1/s1",
        "is_stale": True,
        "supersedes": [],
    },
    {
        "id": "a27",
        "content": "The current API returns JSON payloads.",
        "scope": "t1/p1/c1/a1/s1",
        "supersedes": ["a26"],
    },
]

QUERIES: list[dict[str, object]] = [
    # fact / paraphrase
    {
        "id": "aq1",
        "kind": "fact",
        "query": "Who invented COBOL?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a01", "weight": 1.0}],
    },
    # typo noise: "pipleline" + dropped punctuation
    {
        "id": "aq2",
        "kind": "fact",
        "query": "deploy pipleline runs tests lints builds",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a06", "weight": 1.0}],
    },
    # paraphrase (semantic zone)
    {
        "id": "aq3",
        "kind": "paraphrase",
        "query": "How is authentication enforced on inbound requests?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a08", "weight": 1.0}],
    },
    # contradiction: current region (a12 final) — a10 superseded by a11, a11 stale, a12 active
    {
        "id": "aq4",
        "kind": "correction",
        "query": "Which region is the service deployed to?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a12", "weight": 1.0}],
    },
    # decoy: Q3 revenue (a13) vs Q4 revenue (a14) vs headcount (a15)
    {
        "id": "aq5",
        "kind": "fact",
        "query": "What is the revenue target for Q4?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a14", "weight": 1.0}],
    },
    # decoy: a15 has "Q3 twelve" but asks headcount
    {
        "id": "aq6",
        "kind": "fact",
        "query": "What is the headcount target for Q3?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a15", "weight": 1.0}],
    },
    # cross-scope: a16 is in t2, a17 in t1 — must surface a17, never a16
    {
        "id": "aq7",
        "kind": "fact",
        "query": "When is Project Chrysalis launching?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a17", "weight": 1.0}],
    },
    # multi-source: signing ceremony + contract signed — expect both
    {
        "id": "aq8",
        "kind": "multi_source",
        "query": "What do we know about the Acme contract signing?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a18", "weight": 1.0}, {"document_id": "a19", "weight": 0.8}],
    },
    # long-tail vocabulary
    {
        "id": "aq9",
        "kind": "fact",
        "query": "What quantisation group size does the scheme use?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a21", "weight": 1.0}],
    },
    # near-duplicate: a22 is canonical, a23 superseded by it. The probe
    # expects a22 (dedup/consolidation is the dreaming service's job).
    {
        "id": "aq10",
        "kind": "fact",
        "query": "When does the nightly backup run?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a22", "weight": 1.0}],
    },
    # negative / abstention: nothing about a specific topic exists
    {
        "id": "aq11",
        "kind": "negative",
        "query": "What is the salary of the new CFO?",
        "scope": "t1/p1/c1/a1/s1",
        "answerable": False,
        "expected_evidence": [],
    },
    # adversarial: query framed to lure the decoy (a13 "twelve million" vs a14)
    {
        "id": "aq12",
        "kind": "adversarial",
        "query": "Revenue in Q4 — how much?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a14", "weight": 1.0}],
    },
    # temporal present: current API
    {
        "id": "aq13",
        "kind": "temporal",
        "query": "What format does the API return today?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a27", "weight": 1.0}],
    },
    # temporal past
    {
        "id": "aq14",
        "kind": "temporal",
        "query": "What format did the old API return?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a26", "weight": 1.0}],
    },
    # current password manager (present-oriented temporal)
    {
        "id": "aq15",
        "kind": "temporal",
        "query": "How are secrets stored by the password manager today?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a04", "weight": 1.0}],
    },
    # stale decoy lure: old password manager (must surface a05 only for past framing)
    {
        "id": "aq16",
        "kind": "temporal",
        "query": "How were secrets stored by the old password manager?",
        "scope": "t1/p1/c1/a1/s1",
        "expected_evidence": [{"document_id": "a05", "weight": 1.0}],
    },
]


def main() -> None:
    manifest = {
        "schema_version": 1,
        "manifest_id": "adversarial-v1-2026-08-05",
        "name": "Adversarial-27 (A-3 option 3)",
        "corpus": {"id": "adversarial-27", "version": "1.0", "seed": 20260805, "documents": DOCS},
        "queries": QUERIES,
        "providers": [
            {
                "id": "severian-sqlite",
                "kind": "severian",
                "vendor": None,
                "deployment": "local",
                "backend": "sqlite",
                "model": {"id": "hash", "prompt_version": "manifest-v1"},
                "config": {},
            },
            {
                "id": "severian-postgres",
                "kind": "severian",
                "vendor": None,
                "deployment": "local",
                "backend": "postgresql",
                "model": {"id": "hash", "prompt_version": "manifest-v1"},
                "config": {},
            },
            {
                "id": "chroma",
                "kind": "external",
                "vendor": "chroma",
                "deployment": "self-hosted",
                "backend": None,
                "model": {"id": "chroma-default", "prompt_version": "n/a"},
                "config": {},
            },
            {
                "id": "baseline-lexical",
                "kind": "external",
                "vendor": "Baseline (bag-of-words)",
                "deployment": "self-hosted",
                "backend": None,
                "model": {"id": "bm25", "prompt_version": "n/a"},
                "config": {},
            },
        ],
        "metrics": [
            "recall_at_10",
            "mrr",
            "ndcg_at_10",
            "citation_precision",
            "stale_fact_rate",
            "temporal_accuracy",
            "cross_scope_leakage",
            "unsupported_answer_rate",
            "latency_p95_ms",
            "cost_per_query_usd",
        ],
        "release_gate": {
            "min_recall_at_10": 0.95,
            "min_citation_precision": 1.0,
            "max_stale_fact_rate": 0.0,
            "max_cross_scope_leakage": 0.0,
            "max_unsupported_answer_rate": 0.0,
            "require_external_providers": 1,
        },
    }
    OUT.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {OUT} ({len(DOCS)} docs, {len(QUERIES)} queries)")


if __name__ == "__main__":
    main()
