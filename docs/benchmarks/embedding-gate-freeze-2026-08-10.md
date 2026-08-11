# Embedding bake-off gate freeze — 2026-08-10 (round-two remediation)

Status: **FROZEN BEFORE ANY MODEL RUN.** These thresholds, metric
semantics and resource caps are pre-registered for the round-two
embedding bake-off. They will NOT be edited after lane results are
seen, and will not be tuned to force a winner (handoff §3 / P0-D).

## Corrected metric semantics (P0-D)

The audited gate divided answerability failures by ALL queries
(26/120 = 0.2167 passed the old 0.22 ceiling) and coupled citation
precision across populations. The round-two runner
(`src/severian/benchmarks/runner.py`) computes population-correct
metrics:

| Metric | Denominator |
|---|---|
| `recall_at_10`, `mrr`, `ndcg_at_10`, `citation_precision`, `unsupported_answer_rate` | answerable queries ONLY |
| `negative_false_positive_rate` | unanswerable queries ONLY (any returned result for an unanswerable probe = false positive) |
| `stale_fact_rate`, `cross_scope_leakage` | returned items (temporal queries exempt from stale counting) |

A lane that returns results for every unanswerable probe reports
`negative_false_positive_rate = 1.0`, never `N_unanswerable/N_total`.
Missing mandatory populations invalidate the manifest and fail the
runner loudly — never a convenient zero.

## Frozen release gate (docs/benchmarks/manifest-embedding-v1.json)

| Threshold | Value | Basis |
|---|---|---|
| `min_recall_at_10` | 0.90 | handoff P0-D: "Preserve recall >= 0.90" |
| `min_citation_precision` | 0.78 | frozen pre-run; unchanged from prior corrected gate |
| `max_stale_fact_rate` | 0.0 | locked contract |
| `max_cross_scope_leakage` | 0.0 | locked contract |
| `max_unsupported_answer_rate` | 0.1 | corrected semantics: answerable-abstention ceiling, frozen pre-run |
| `max_negative_false_positive_rate` | 0.0 | corrected semantics: an abstaining system must return nothing for unanswerable probes |
| `require_external_providers` | 0 | embedding bake-off is an internal lane comparison |

## Frozen resource caps (P0-F)

Applied INSIDE each fresh lane subprocess
(`src/severian/benchmarks/embedding_runner.py` `_apply_resource_caps`):

- `cpus`: 4 — process pinned to 4 logical CPUs (affinity).
- `mem_mb`: 4096 — hard `RLIMIT_AS` cap (MiB); a lane that exceeds it
  dies with `MemoryError` and is recorded as SKIPPED, never faked.

Declared per neural provider in the manifest `config`. The hash
baseline (control) runs uncapped (pure-Python, trivial footprint).

## Corpus (P0-E)

All lanes run the SAME frozen round-two corpus:
`docs/benchmarks/corpus/quality-corpus-v1.json`
(content sha `b836ea6016f7df44606c22d7624b325f4be1855fdd5e4dee73a7b6bfaeb5d428`,
file sha `83f4e4b2383f17907dec36825d4c9b86cfb593f1f19df55642d3edb027a97fb9`),
with the same query/evidence mappings, a fresh subprocess per lane, the
exact immutable model revisions, and separate quality/throughput
indexes. Provenance: `docs/benchmarks/manifest-quality-corpus-v1.json`.

## Schema v2 manifest notes

`docs/benchmarks/manifest-embedding-v1.json` was bumped to
`schema_version: 2` with `negative_false_positive_rate` added to
`metrics` and `max_negative_false_positive_rate` to `release_gate`
(metric-meaning change → schema bump, never silent reinterpretation).
Lane matrix per handoff §8: Qwen3 Embedding 0.6B at 1024/512/384 with
the pinned query-instruction prompt (`Instruct: …\nQuery:`), matched
Qwen no-instruction controls, Granite 311M R2 at 768/384, Granite
107M/97M controls, Bekko A8M, English-only Granite (control only),
Gemma 300M only if already authorised, hash baseline (control only,
never selectable). Jina remains excluded.
