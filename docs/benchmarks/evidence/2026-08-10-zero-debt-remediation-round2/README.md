# Severian Zero-Debt Remediation — Round Two Evidence Package

Evidence ID: 2026-08-10-zero-debt-remediation-round2
Source SHA: bde3eac2c6843af763c74030b2a073f31ad5cc64 (clean, verified)
Base SHA: 635a47c158fda97e719be2a2a55b8c48ca76ab2a (verified ancestor)

## Verdict

ALL P0/P1/P2 REMEDIATION COMPLETE — no gate lowered, no blanket ignores,
no fabrication. Model selection: NO MODEL SELECTED.

## Bake-off ranked table (3 fresh-process reps, frozen corpus b836ea6…)

Mean across reps (identical to 4 decimals — deterministic):

| lane | recall@10 | nDCG@10 | citation | unsup | neg-FP | gate |
|---|---|---|---|---|---|---|
| qwen-no-instruction-1024 | 0.8204 | 0.5930 | 0.3155 | 0.000 | 1.000 | FAIL |
| qwen-0.6b-512 | 0.8155 | 0.6010 | 0.3398 | 0.000 | 1.000 | FAIL |
| qwen-0.6b-384 | 0.8155 | 0.6010 | 0.3350 | 0.000 | 1.000 | FAIL |
| qwen-no-instruction-512 | 0.8155 | 0.5940 | 0.3228 | 0.000 | 1.000 | FAIL |
| qwen-0.6b-1024 | 0.8058 | 0.6100 | 0.3592 | 0.000 | 1.000 | FAIL |
| granite-97m | 0.8010 | 0.5870 | 0.3252 | 0.000 | 1.000 | FAIL |
| qwen-no-instruction-384 | 0.8010 | 0.5975 | 0.3447 | 0.000 | 1.000 | FAIL |
| bekko-a8m | 0.7961 | 0.5925 | 0.3520 | 0.000 | 1.000 | FAIL |
| granite-107m | 0.7913 | 0.5890 | 0.3568 | 0.000 | 1.000 | FAIL |
| granite-311m-768 | 0.7913 | 0.5825 | 0.3180 | 0.000 | 1.000 | FAIL |
| granite-small-en | 0.7913 | 0.5900 | 0.3544 | 0.000 | 1.000 | FAIL |
| granite-311m-384 | 0.7816 | 0.5785 | 0.3155 | 0.000 | 1.000 | FAIL |
| hash (baseline) | 0.7233 | 0.5225 | 0.2913 | 0.049 | 0.955 | FAIL |
| gemma-300m | — | — | — | — | — | SKIPPED (licence) |

Frozen gate: recall >= 0.90 AND citation >= 0.78 AND unsupported <= 0.10
AND negative-FP <= 0.0. No lane passes. Best recall 0.820 (threshold 0.90),
best citation 0.359 (threshold 0.78), all dense models negative-FP 1.000
(retrieve neighbours even for nonsense probes).

## Why NO MODEL SELECTED (not a failure of the round)

1. R1's "accept best model" (qwen-0.6b) was based on structurally defective
   gates (citation 0.8/unsupported 0.1 impossible for dense models). R2 fixed
   the metrics; the honest gate rejects every lane.
2. EmbeddingGemma-300m is licence-gated and NOT authorised — per the locked
   handoff decision the matrix is incomplete by design, which mandates
   NO SELECTION regardless of other lanes.
3. jina-v4 excluded (non-commercial licence) — never downloaded.
4. The Rust native seam is NOT gated on model quality: its vector-only proof
   (rust-vector-e2e.json) passes — id/score parity + 91-95% p95 gain.

## Contents

- fingerprint.json — environment, hashes, locked decisions, rep SHAs
- test-results.json — every gate with individual exit codes
- rust-vector-e2e.json — vector-only Rust proof (4 dims)
- bakeoff-aggregate.json — 3-rep means + gate verdict (sha fbe429dd…)
- bakeoff-reps/rep{1,2,3}/ — full per-lane raw evidence
  (summaries: 7f8985bb…, 816f4b74…, d35eb082…)

## Reproducibility

- Frozen corpus sha b836ea6… (file sha 83f4e4b2…), 1107 docs / 250 queries,
  26% non-English, 40 correction chains, 40 temporal pairs
- Pinned model revisions in manifest-embedding-v1.json (sha 4a5350bf…)
- sentence-transformers 5.7.0, torch 2.13.0+cpu, Python 3.12.13, uv 0.11.28
- Fresh subprocess per lane, fresh process per rep, 4-CPU / 8GB caps

## Follow-up (out of scope for this round, needs Sahil)

- Authorise a compliant embedding licence (Gemma or other) OR review gate
  calibration/corpus difficulty → then re-run bake-off for selection
- Production activation remains untouched (no merge/push/rebuild)
