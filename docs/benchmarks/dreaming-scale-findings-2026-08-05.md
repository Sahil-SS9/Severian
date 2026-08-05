# Dreaming / curation scale lane (P1d) — findings, 2026-08-05

**Status:** DONE. Evidence: `docs/benchmarks/evidence/2026-08-05/dreaming-scale.json`.
Runner: `scripts/run_dreaming_scale.py` (deterministic seeded synthetic,
incremental evidence writes).

## Measured cost curve (SQLite backend, same machine)

| Bucket | Consolidate | Consolidate /s | Curation | Curation /s |
|---|---|---|---|---|
| 1,000 | 6.28 s | 159.2 | 2.31 s | 432.1 |
| 10,000 | 437.00 s | 22.9 | 234.91 s | 42.6 |
| **10x data** | **~70x slower** | — | **~102x slower** | — |

Corpus: ~10% near-duplicate pairs per bucket (the consolidation target).
At 1k: 50 duplicate groups merged; at 10k: 500 merged — linear in the
duplicate fraction, as designed.

## Findings (honest)

1. **Consolidation is O(n²) pairwise** — confirmed. 10x data → 70x time.
   The 10k Jaccard pass is ~50k pair-comparisons/s effective. Predicted
   6.28s x 100 = 628s; measured 437s (pairwise short-circuits on token-set
   size make it sub-n² in constant but the curve is unmistakably
   super-linear).

2. **Curation is ALSO O(n²), not O(n)** — the scoring loop is O(n), but
   `CurationService.curate` reuses `ConsolidationService._find_contradictions`
   (pairwise) for the contradiction-proposal list. Measured 102x slowdown
   at 10k. The original honest_notes claim ("curation is O(n)") was
   WRONG and has been corrected in the runner.

3. **Practical ceiling today:** ~10k memories per scope for a full dream
   pass (consolidate + curate ≈ 11 min). Beyond that the dream worker
   becomes the bottleneck. The system does NOT fail — it just takes
   longer. Non-destructive guarantees hold at both buckets (no memory
   deleted; review markers only).

## Recommended optimization (not implemented in this turn — evidence first)

The O(n²) comes from all-pairs Jaccard + all-pairs contradiction
overlap. A min-hash / shingling pre-filter (banded LSH) reduces the
pairwise candidate set by ~2-3 orders of magnitude at 10k+ while
preserving the >= 0.8 Jaccard threshold exactly (LSH is a recall
pre-filter, not an approximation of the merge decision). Contradiction
scan can reuse the same candidate set (both are token-overlap based).

Cost/benefit: LSH pre-filter is a bounded, deterministic, testable
change to ConsolidationService (new candidate-generation seam). The
merge decision itself stays exact Jaccard — no quality tradeoff, only
a candidate-recall tradeoff that the bench probes would catch.

Decision: DO NOT ship the optimization blind. The scale lane exists to
measure; the follow-up (P1d-opt) is to implement LSH pre-filtering with
the same lane proving the curve flattens. Tracked in kanban.
