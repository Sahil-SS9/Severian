# Phase D bake-off FINAL VERDICT — 3 fresh-process reps (2026-08-09)

Corpus: sha d463453e642ece0f (query-gen defect fixed)
Gate: corrected constants (recall 0.9 / citation 0.78 / unsupported 0.22;
      stale 0.0 / leakage 0.0) — see 05-corpus-query-defect.md for derivation
Backend: SEVERIAN_VECTOR_BACKEND=python (identical retrieval semantics per lane)
Protocol: 3 fresh-process repetitions, sequential (no CPU contention);
          fresh subprocess per lane with true peak RSS (regression-tested)

## Consolidated results (median of 3 reps)

| lane | recall@10 | ndcg@10 | citprec | unsup | p95ms | RSS_MB | gate |
|------|-----------|---------|---------|-------|-------|--------|------|
| hash (control) | 0.681 | 0.563 | 0.550 | 0.000 | 68.2 | 1044 | FAIL |
| granite-107m | 0.787 | 0.640 | 0.383 | 0.217 | 81.7 | 1981 | FAIL |
| granite-97m | 0.766 | 0.639 | 0.400 | 0.217 | 77.2 | 1826 | FAIL |
| qwen-0.6b | 0.787 | 0.636 | 0.383 | 0.217 | 170.5 | 2341 | FAIL |
| qwen-no-instruction | 0.745 | 0.611 | 0.383 | 0.217 | 168.5 | 2341 | FAIL |
| granite-311m | 0.777 | 0.626 | 0.350 | 0.217 | 126.3 | 2067 | FAIL |
| bekko-a8m | 0.777 | 0.618 | 0.525 | 0.083 | 79.5 | 1846 | FAIL |
| granite-small-en | 0.766 | 0.636 | 0.392 | 0.217 | 83.1 | 1731 | FAIL |
| gemma-300m | — | — | — | — | — | — | SKIPPED (401 licence-gated) |

## Reproducibility (recall min-max across 3 reps)

- hash: 0.681-0.691 (near-deterministic)
- granite-107m: 0.777-0.798
- granite-97m: 0.766-0.777
- qwen-0.6b: 0.787-0.798
- granite-311m: 0.766-0.777
- bekko-a8m: 0.777 (exact across reps)
- Max spread: 0.021 (granite-107m) — tight, ranking stable

## VERDICT

NO LANE PASSES THE RELEASE GATE. This is a REAL finding on the corrected
harness: the corpus (real conversational profile-store content, 24/94
answerable queries with anchors shared by >10 scope docs) is genuinely
hard, and no local embedding candidate reaches recall 0.9 / citation 0.78.

The gate is NOT bypassed and evidence IS usable for RELATIVE ranking
(Phase D item: the runner's gate-aware table, audit CRITICAL 2 fix).

## Relative ranking (informational, NOT tier selection)

1. qwen-0.6b: best recall (0.787) tied with granite-107m, but 2.1-2.2x
   slower than granite-97m (170.5 vs 77.2ms p95) and heaviest (2341MB)
2. granite-107m: recall 0.787 at 81.7ms — best quality-per-latency in
   recall terms, but nDCG 0.640 vs granite-97m 0.639 (tied)
3. granite-97m: best nDCG-per-latency (0.639 @ 77.2ms), lowest dense RSS
4. bekko-a8m: most "decline-able" (unsup 0.083, citprec 0.525) — returns
   fewer results; not a quality advantage
5. qwen-no-instruction 0.745 < qwen-0.6b 0.787: instruction prompt HELPS
   (+0.042 recall) — prompt separation meaningful
6. hash control 0.681: cleanly separated from all neural lanes

## Tier-selection implication

No tier is selected by this bake-off. Model-tier selection must either
(a) accept a lower quality bar with the relative ranking above, or
(b) be deferred pending stronger candidates. Both are explicit decisions
for Sahil — NOT auto-decided by this builder.

## Evidence files

- /tmp/rem-phase-d-bakeoff/            rep-1 (embedding-bakeoff.json + lane-*.json)
- /tmp/rem-phase-d-bakeoff-rep2/       rep-2
- /tmp/rem-phase-d-bakeoff-rep3/       rep-3
- /tmp/rem-phase-d-bakeoff{,-rep2,-rep3}.log
