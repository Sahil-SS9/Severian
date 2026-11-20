# Benchmark methodology

This page defines how Severian benchmark numbers are produced, labelled and
reproduced. It separates public benchmark runs from internal quality gates and
from historical August-era instruments.

## 1. Layers

### Public benchmark layer

OmniMemEval is the public, cross-system layer used for the October 2026
leaderboard round. Severian is run through OmniMemEval's own harness and client
factory, with the same questions, answer model, judge model and prompts as the
other systems. Headline numbers are in [RESULTS.md](RESULTS.md); the full
tables are in [omnimemeval-2026-10.md](omnimemeval-2026-10.md).

### Internal quality-gate layer

`manifest-v1.json` drives the in-repo quality gate: synthetic documents with
known expected evidence, corrections, negative queries and five-dimensional
scope cases. It measures recall@10, MRR, nDCG@10, citation precision,
stale-fact rate, temporal accuracy, cross-scope leakage and unsupported-answer
rate. It is a release gate, not a claim about public leaderboard rank.

### Internal scale layer

The scale lane regenerates a deterministic corpus at 1k, 10k and 100k documents
from a fixed seed and measures ingest throughput and cold/hot query latency.
These are engineering measurements, not retrieval-quality scores.

### Historical layer

August-era instruments were built before the public-benchmark round and are
retained under [history/](history/). They use different corpora, manifests and
gate semantics. They are provenance, not current results.

## 2. Fairness rules

1. **Same questions.** Paired comparisons pair the exact question keys used by
   both systems. The recompute script refuses a run where the two systems were
   asked different question sets or where paired question text differs.
2. **Same answer and judge path.** Severian and the comparison system use the
   same OmniMemEval answer model, judge model and templates. The memory/write
   model is part of each system's configuration, not the shared answer path.
3. **Configurations are labelled.** A number is only valid with the version,
   commit, retrieval depth, context cap and switch set that produced it. The
   board configuration (top-20, no cap) and the Hermes shipped configuration
   (top-10, 8,000 characters) are separate rows.
4. **No relabelling.** A later byte-identical version does not inherit a run's
   label unless a source diff confirms the relevant path is unchanged. 0.3.12
   and 0.3.13 remain labelled as 0.3.11 results for this reason.
5. **Paired uncertainty.** Differences use a paired per-question bootstrap
   (10,000 resamples, seed 20261023, 2.5/97.5 percentiles) against the same
   comparison system. The board's absolute scores have no published ranges.
6. **Report the failures.** No failed mark is silently dropped. Failed model
   calls and excluded questions are stated with the run. The BEAM marking flaw
   is disclosed even though it inflates Severian too.
7. **No benchmark-driven tuning on the test set.** Changes are pre-registered
   or disclosed as post-hoc; the October round's amendments and incidents are
   recorded in [omnimemeval-2026-10.md](omnimemeval-2026-10.md).

## 3. Configurations

| Label | Severian | Retrieval | Context | Use |
|---|---|---|---|---|
| Board depth | 0.3.11, commit `4467853` | top-20 | no cap | OmniMemEval headline rows |
| Hermes default | 0.3.11 | top-10 | 8,000 chars | What Hermes ships |
| Higher cap | 0.3.11 + fill/compact | top-10 | 16,000 chars | Pre-registered side test |

The live switch set for the board run: `SEVERIAN_USER_FACTS=1`,
`SEVERIAN_DATE_STAMPS=1`, `SEVERIAN_TRUTH_RESOLUTION=1`,
`SEVERIAN_OPT_NUMPY=1`, `SEVERIAN_RERANK=cross-encoder/ms-marco-MiniLM-L6-v2`,
`SEVERIAN_CONFLICT_NOTES=1`, local Qwen3-Embedding-0.6B, `TOPK=20`.

Answer model: `openai/gpt-4.1-mini`. Judge: `openai/gpt-4o-mini-2024-07-18`
(plus the Claude Opus strict-marker side check). These are model and harness
choices, not claims that another model or judge would reproduce the same
absolute scores.

## 4. How to reproduce

1. Verify the archive directory:
   `cd ~/data/benchmarks/_records/omnimemeval-2026-10 && sha256sum -c SHA256SUMS`.
2. Run the offline recompute:
   `python docs/benchmarks/omnimemeval/recompute-2026-10.py --archive <results-per-question.tar.zst>`.
3. The script verifies
   `docs/benchmarks/omnimemeval/MANIFEST-results-per-question-2026-10.sha256`,
   recomputes the headline scores, category counts/means and paired ranges, and
   compares them with `docs/benchmarks/omnimemeval/results-2026-10.json`.
4. A correct run ends with
   `PASS: 87.34 / 90.60 / 71.62 and all paired ranges and category rows match results-2026-10.json`.

The script uses only the Python standard library plus the system `tar` with
zstd support. It makes no network calls and no model or paid calls.

The per-question outputs are not redistributed because the dataset licence and
redistribution terms for the derived judged files are not established from the
archive. The manifest, hashes and script are sufficient to verify the numbers
against a locally held copy of the archive.

## 5. Metric definitions

- **LoCoMo / LongMemEval score:** mean binary mark over all questions, expressed
  as a percentage. A mark is the judge's answer-correctness decision.
- **BEAM score:** plain mean of `nugget_score` over all rubric-scored questions,
  including partial credit. It is not a pass rate.
- **Paired difference:** mean per-question score difference, in percentage
  points, with the paired bootstrap range described above.
- **Internal gate metrics:** recall@10, MRR, nDCG@10, citation precision,
  stale-fact rate, temporal accuracy, cross-scope leakage and unsupported-answer
  rate, as defined by `manifest-v1.json` and the runner.

The nDCG@10 denominator uses all expected items, not the returned top-k; the
audit is recorded in [RESULTS.md](RESULTS.md).

## 6. Change control

A benchmark result is re-run, not relabelled, when any of these change: the
retrieval/ranking path, context assembly, derivation model, embedding model,
judge model, answer model, harness pin, or the question set. Configuration-only
results may be added beside an existing run only when the configuration is
explicit and the comparison uses the same questions.

## 7. Index

- [RESULTS.md](RESULTS.md) — canonical headline numbers and reconciliation.
- [omnimemeval-2026-10.md](omnimemeval-2026-10.md) — full October round.
- [competitor-round-2026-10.md](competitor-round-2026-10.md) — earlier internal
  cross-system round.
- [provider-benchmark.md](provider-benchmark.md) — in-repo runner and manifest
  format.
- [history/](history/) — August-era instruments, historical only.
