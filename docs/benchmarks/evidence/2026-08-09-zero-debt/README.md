# Evidence — zero-debt embedding performance candidate (2026-08-09)

> **SUPERSEDED — INVALID FOR MODEL SELECTION** (2026-08-10 round-two remediation)
>
> Historical packet for the 2026-08-09 zero-debt candidate (SHA `c92769c`,
> uncommitted state, dirty base `cab8088`). It ran the OLD real-data corpus
> (`de09ea4e…`) under the OLD gate semantics and is NOT valid model-selection
> evidence. Raw artefacts below (`fingerprint.json`, `result.json`) are
> preserved IMMUTABLE. **NO MODEL SELECTED** as of 2026-08-10; round-two
> replacement evidence will be published in a new dated directory.

SHA: `c92769ca2e8cb70684cef47e0b38f9605c369cfb` (branch `zero-debt-embedding-perf`)

## What this packet contains

- `fingerprint.json` — candidate identity, corpus hash, embedding lanes.
- `result.json` — scale evidence (1K/10K/100K) + embedding bake-off quality/latency.

## Scale evidence

Ingest throughput (docs/s) and query latency (ms), complete warmup unless noted.

| Bucket | Lane | docs/s | p50 ms | p95 ms | Warmup |
|---|---|---|---|---|---|
| 1K | severian-sqlite | 245.8 | 45.5 | 63.4 | complete |
| 1K | severian-postgres | 151.0 | 53.7 | 98.6 | complete |
| 10K | severian-sqlite | 243.7 | 370.4 | 545.0 | complete |
| 10K | severian-postgres | 148.5 | 66.6 | 108.8 | complete |
| 100K | severian-sqlite | 186.0 | 1368.6 | 1470.9 | **partial** (35.5K warmed) |
| 100K | severian-postgres | 154.2 | 65.8 | 151.7 | **partial** (47.5K warmed) |

The 100K lane was not fully warmed on this run; those numbers are indicative,
not the final 100K claim. The only complete-warmup 100K packet in-repo is
`docs/benchmarks/evidence/2026-08-03/scale-100k.json` (sqlite 205.9 docs/s,
postgres 159.9 docs/s) but it was produced on an OLDER SHA (`e6ead47`), so it
is trend context, not evidence for this candidate. A complete-warmup 100K run
on `c92769c` remains outstanding if the final 100K claim is required.

## Embedding bake-off (real-data corpus)

Corpus sha256: `de09ea4e482de39b8946430fec14dcc8b5d7e67839d85da2d31e0203f5904839`
(redacted; redaction applied pre-write, post-scan shows zero PII patterns).

| Lane | model | dim | recall@10 | MRR | nDCG@10 | p95 ms | docs/s |
|---|---|---|---|---|---|---|---|
| granite-311m | ibm-granite/granite-embedding-311m-multilingual-r2 | 768 | 0.9915 | 0.9066 | 0.9274 | 146.3 | 249.9 |
| granite-small-en | ibm-granite/granite-embedding-small-english-r2 | 384 | 0.9915 | 0.9149 | 0.9337 | 149.9 | 241.9 |

Skipped lanes (no quality run recorded in this packet): hash (deterministic
fallback baseline), granite-107m, granite-97m, gemma-300m, qwen-0.6b,
bekko-a8m.

## Provenance

- Scale runner: in-repo `scripts/scale_runner.py`; warmup-complete flags copied
  verbatim from the runner's JSON.
- Bake-off: in-repo embedding bake-off runner; values copied verbatim from
  `/tmp/zero-debt-embedding-r2/embedding-bakeoff.json` (run on this SHA).
- No values were re-computed or adjusted; the partial-warmup caveat above is
  the only editorial annotation.
