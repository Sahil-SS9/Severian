# Severian — Third-Party Objective Evaluation Prompt

> **HISTORICAL / SUPERSEDED (2026-08-04).** This prompt was used for the
> 2026-08-03 evaluation at `audit/brutal-critique-v0.1.0`. It predates the
> auditor correction lane (C-01..C-10) and references the pre-merge branch.
> Use `docs/benchmarks/chat-handoff-prompt-2026-08-04.md` for current
> independent review of the correction lane. Preserved for provenance only.

**Purpose:** Independent verification that Severian's benchmark claims are true,
reproducible, and not inflated. This prompt is self-contained: hand it to a
fresh agent session or a human auditor who has never seen the project.

**Repo:** /home/kensei/repos/Severian
**Branch:** audit/brutal-critique-v0.1.0
**Date of claims:** 2026-08-03

---

## Your task

You are an independent auditor. Your job is to verify (or refute) the claims
below by re-running the evidence yourself. You must be adversarial: assume the
claims are wrong until you have reproduced them. Never trust the repo's own
docs — trust only what you can execute and observe.

## The claims to verify

1. **Quality gates:** On the manifest corpus (docs/benchmarks/manifest-v1.json,
   id comparison-v1-2026-08-02), Severian SQLite and Severian PostgreSQL both
   pass all release gates: recall@10 >= 0.95, citation_precision >= 1.00,
   stale_fact_rate == 0.00, cross_scope_leakage == 0.00, unsupported_answer_rate
   == 0.00. There are FIVE configured per-provider release thresholds; MRR and
   nDCG are measured but NOT release-gated. ChromaDB fails three of the five:
   citation_precision, stale_fact_rate and unsupported_answer_rate.
2. **Scale lane:** The numbers in docs/benchmarks/results.md §3 are real —
   ingest throughput ~156-254 docs/s for Severian, ~4 docs/s for Chroma,
   hot-path p95 monotonic non-decreasing when the 1k/10k and 100k evidence
   files are COMBINED (sqlite 69.9 → 382.1 → 3,238.6 ms; PG 83.3 → 98.6 →
   151.6 ms; chroma and baseline only exist at 1k/10k because the 100k chroma
   lane is skipped), cold derive super-linear (9.9s@1k → 149.5s@10k →
   6,458s@100k sqlite).
3. **Anomaly fix:** The 100k numbers are NOT the empty-index artifact. A hot
   search (derive=False) on a never-derived scope returns 0 hits; after one
   cold query (derive=True) it returns real hits. Verify with a 5-doc repro:
   ingest 5 docs → hot search 0 hits in <1ms → one cold query → hot search
   returns 5 hits.
4. **Evidence integrity:** docs/benchmarks/evidence/2026-08-03/scale-1k-10k.json
   and scale-100k.json contain `run_state: complete`, matching lane counts, and
   combined `monotonicity` reports that say p95 is non-decreasing. NOTE:
   scale-100k.json alone records `assessed: false` for monotonicity because it
   contains a single bucket — non-decreasing behaviour is only confirmed by
   combining it with the 1k/10k file. Also NOTE: both files record the git SHA
   of the evidence-producing commit; the incremental-evidence-write change
   landed after those files were produced, so the recorded SHA identifies the
   measurement algorithm but not the exact script state that wrote the file.
   Treat the algorithm as unchanged (verified), the recorded SHA as
   approximate.

## Verification protocol (execute, don't assume)

1. **Environment:** PostgreSQL 16 + pgvector must be reachable at
   postgresql://severian:severian@127.0.0.1:54329/severian. Set
   SEVERIAN_PG_ALLOW_RESET=1. Chroma venv at /tmp/bench-compare-venv (or
   install chromadb 1.5.9 fresh — note CVE-2026-45829, local-only use).
2. **Run the full gate suite:**
   `uv run pytest -q` — expect 228 passed, 36 skipped.
   `uv run mypy --strict src/severian tests/benchmarks scripts/` — expect Success.
   `uv run ruff check src/severian tests/benchmarks scripts/` — expect All checks passed.
3. **Re-run the quality benchmark** (fast, manifest corpus). The command
   REQUIRES the manifest flag — running without it exits 2:
   `uv run python scripts/run_benchmark.py --manifest docs/benchmarks/manifest-v1.json --chroma-venv /tmp/bench-compare-venv --pg-dsn postgresql://severian:***@127.0.0.1:54329/severian --out /tmp/audit-run`
   Compare gate results against claims 1. Both Severian lanes must PASS; chroma
   must FAIL (three of five thresholds).
4. **Re-run a 5-doc anomaly repro THROUGH THE SDK:** write a small script that
   imports the public controller/SDK entrypoint (not bundle.service directly,
   not private module internals) — ingest 5 observations, hot search, cold
   search, hot search again. Record the hit counts and latencies. Confirm the
   0 → N → N pattern. If you cannot drive it through the public SDK cleanly,
   that is a FINDING (the anomaly fix claims SDK behaviour), not an excuse to
   use internals.
5. **Scale lane (time-boxed):** Run `scripts/run_scale_benchmark.py` with
   --min-bucket 1000 --max-bucket 1000 (fast, ~4 min) into a fresh out dir.
   MUST pass --chroma-venv /tmp/bench-compare-venv — omitting it silently
   produces three lanes instead of the protocol's four. Check: evidence file
   written with run_state complete, 4 lanes (sqlite, postgres, chroma,
   baseline), monotonicity present. Do NOT run the 100k bucket unless you have
   4+ hours — the cold derive is genuinely super-linear and that IS the
   finding.
6. **Check the evidence files' internal consistency:** recompute corpus sha256
   (script does it), verify lanes/buckets match the log, verify monotonicity
   numbers match the raw results array.

## Honesty rules (non-negotiable)

- If a claim cannot be reproduced, report exactly what you observed and why —
  do not "fix" the repo to make it pass.
- Never fabricate a number. If a command fails, the failure IS your finding.
- Distinguish clearly: independently reproduced by you vs vendor-reported
  (docs/benchmarks/results.md §5) vs extrapolated. Do not mix them.
- Chroma's 100k lane was deliberately skipped (would take ~9h at ~0.31s/doc).
  That skip is documented in the evidence file's skipped_lanes — it is not a
  hidden omission. Verify the skip reason is recorded, then move on.

## Deliverable format

Return a report with three sections:

1. **VERIFIED** — claims you reproduced, with the commands you ran and the
   exact output (test counts, gate table, repro hit counts).
2. **REFUTED / QUESTIONABLE** — claims you could not reproduce, with the
   observed discrepancy and your best hypothesis for the cause.
3. **GAPS** — claims that are true but weak (e.g. "no semantic embeddings",
   "cold derive super-linear") — list them without softening.

End with a one-line verdict: "Severian's claims are [verified / verified with
exceptions / not verified]" plus the single most important risk you found.
