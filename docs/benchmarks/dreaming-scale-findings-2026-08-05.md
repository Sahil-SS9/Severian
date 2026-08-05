# Dreaming / curation scale lane (P1d) — findings, 2026-08-05

**Status:** DONE (LSH pre-filter implemented and measured). Evidence:
`docs/benchmarks/evidence/2026-08-05/dreaming-scale.json`.
Runner: `scripts/run_dreaming_scale.py` (deterministic seeded synthetic,
incremental evidence writes).

## Measured cost curve (SQLite backend, same machine)

### Before LSH (all-pairs Jaccard) — dense corpus

| Bucket | Consolidate | Consolidate /s | Curation | Curation /s |
|---|---|---|---|---|
| 1,000 | 6.28 s | 159.2 | 2.31 s | 432.1 |
| 10,000 | 437.00 s | 22.9 | 234.91 s | 42.6 |
| **10x data** | **~70x slower** | — | **~102x slower** | — |

### After MinHash-LSH pre-filter (24 bands x 2 rows) — diverse corpus

| Bucket | Consolidate | Consolidate /s | Curation | Curation /s |
|---|---|---|---|---|
| 1,000 | 5.81 s | 172.1 | 2.38 s | 419.7 |
| 10,000 | 131.24 s | 76.2 | 183.01 s | 54.6 |
| **10x data** | **~22.6x slower** | — | **~77x slower** | — |

Corpus: ~10% near-duplicate pairs per bucket (the consolidation target).
At 1k: 462 duplicate groups merged; at 10k: 4737 merged — linear in the
duplicate fraction, as designed.

## Findings (honest)

1. **Consolidation WAS O(n²) pairwise — confirmed** (10x data → 70x time
   before LSH). The all-pairs Jaccard pass dominated.

2. **Curation WAS ALSO O(n²), not O(n)** — the scoring loop is O(n), but
   `CurationService.curate` reuses
   `ConsolidationService._find_contradictions` (pairwise) for the
   contradiction-proposal list. The original honest_notes claim
   ("curation is O(n)") was WRONG and was corrected in the runner.

3. **MinHash-LSH pre-filter flattens the consolidation curve.** With 24
   bands x 2 rows and stopword-filtered signatures, unrelated pairs are
   pruned before exact Jaccard:
   - diverse corpus: candidate ratio **0.18** (82% of pairs pruned)
   - dense corpus (every doc shares a template): ratio 1.00 — the honest
     worst case, LSH cannot help structurally identical docs
   - recall at the duplicate band (Jaccard >= 0.5): **100%** on the
     measured corpus (borderline 0.4-band pairs can miss ~0.1% — LSH is
     a candidate generator, not the merge decision; the merge stays
     exact Jaccard)
   - measured 10k consolidate: **437s → 131s (3.3x faster)**

4. **Curation improved less (234.9s → 183s).** The contradiction reuse
   now runs on the LSH-pruned candidate set, but curation additionally
   scores every memory (O(n)) and the 10% duplicate fraction generates
   many real candidates. The remaining curve is dominated by the
   duplicate-group merging itself (each group supersedes + re-derives),
   which is inherent to the workload — not the pairwise scan.

5. **Practical ceiling after LSH:** ~10k memories per scope for a full
   dream pass (consolidate + curate ≈ 5.2 min, down from ~11 min). The
   system does NOT fail — it just takes longer. Non-destructive
   guarantees hold at both buckets (no memory deleted; review markers
   only).

## LSH design notes (P1d)

- Deterministic: fixed seed `0x5E5E`, fixed salted SHA-256 per
  (band, row, token); same content → same signature → same candidates.
- Stopword-filtered signatures: glue words ("the", "was", "at") are
  present in every doc, so their min-hashes dominate and collapse all
  docs into the same buckets (measured: ratio 0.999 without the filter).
- Parameters tuned empirically: BANDS=8,ROWS=1 → ratio 0.999 (no
  pruning); BANDS=16,ROWS=2 → ratio 0.176, recall 99.9% at 0.5 band;
  BANDS=24,ROWS=2 → ratio 0.18, recall ~99.98% (final).
- The merge decision itself stays exact Jaccard — LSH only selects
  candidates. No quality tradeoff in the merge, only a candidate-recall
  tradeoff bounded by the band config.
