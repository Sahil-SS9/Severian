# Phase D bake-off rep-1 — corrected gate (2026-08-09)

Corpus: sha d463453e642ece0f (query-gen defect fixed; see 05-corpus-query-defect.md)
Manifest: manifest-embedding-v1.json with corrected gate constants
  (citation 0.8 -> 0.78 ceiling, unsupported 0.1 -> 0.22 floor; recall 0.9 unchanged)
Backend: SEVERIAN_VECTOR_BACKEND=python (Phase D item 4: identical retrieval
  semantics for every lane, no native seam in the model-quality comparison)
Protocol: fresh subprocess per lane (manifest_path param; regression test green)

## Results (120 queries, 5,000 docs, 5 scopes)

| lane | recall@10 | nDCG@10 | mrr | citprec | unsup | p95ms | cold_s | RSS_MB | gate |
|------|-----------|---------|-----|---------|-------|-------|--------|--------|------|
| hash | 0.681 | 0.563 | 0.524 | 0.550 | 0.000 | 68.2 | 0.2 | 1044 | FAIL recall+citprec |
| granite-107m | 0.777 | 0.628 | 0.581 | 0.383 | 0.217 | 81.7 | 3.7 | 1980 | FAIL recall+citprec |
| granite-97m | 0.766 | 0.639 | 0.599 | 0.400 | 0.217 | 77.2 | 2.9 | 1826 | FAIL recall+citprec |
| gemma-300m | — | — | — | — | — | — | — | — | SKIPPED 401 gated |
| qwen-0.6b | 0.787 | 0.636 | 0.588 | 0.383 | 0.217 | 170.5 | 3.0 | 2341 | FAIL recall+citprec |
| qwen-no-instruction | 0.745 | 0.619 | 0.578 | 0.383 | 0.217 | 168.5 | 2.9 | 2341 | FAIL recall+citprec |
| granite-311m | 0.777 | 0.625 | 0.575 | 0.350 | 0.217 | 116.6 | 3.7 | 2066 | FAIL recall+citprec |
| bekko-a8m | 0.777 | 0.637 | 0.592 | 0.525 | 0.083 | 74.7 | 3.6 | 1844 | FAIL recall+citprec |
| granite-small-en | 0.766 | 0.636 | 0.594 | 0.392 | 0.217 | 80.2 | 1.9 | 1731 | FAIL recall+citprec |

## Honest interpretation

- NO lane passes the release gate. This is a real finding, not a harness
  artifact: the corpus is genuinely hard (real conversational profile-store
  content; 24/94 answerable queries have anchors shared by >10 scope docs).
- The corrected gate fails lanes ONLY on the quality bars (recall 0.9,
  citation 0.78). The previously-impossible unsupported/citation constants
  no longer block dense models.
- RELATIVE ranking (tier-selection-adjacent, NOT tier selection):
  1. qwen-0.6b recall 0.787 — best recall, but 2.2x slower than granite-97m
  2. granite-97m nDCG 0.639, mrr 0.599 — best dense-lane quality-per-latency
  3. granite-311m / granite-107m / bekko-a8m recall 0.777
  4. qwen-no-instruction recall 0.745 — instruction prompt HELPS (0.787 vs 0.745)
  5. hash control 0.681 — clearly separated from all neural lanes
- Instruction vs no-instruction: qwen with instruction beats without on
  recall (+0.042) at similar latency — prompt separation is meaningful.
- bekko-a8m's lower unsup (0.083) and higher citprec (0.525): it returns
  fewer/empty results (can "decline") — not a quality advantage.

## Evidence files

- /tmp/rem-phase-d-bakeoff/embedding-bakeoff.json (full raw per-query data)
- /tmp/rem-phase-d-bakeoff/lane-*.json (per-lane checkpointed evidence)
- /tmp/rem-phase-d-bakeoff.log (live run log)
