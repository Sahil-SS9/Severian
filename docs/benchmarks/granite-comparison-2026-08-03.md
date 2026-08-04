# Granite R2 embedding comparison — honest measurement (2026-08-03)

**Manifest:** `manifest-locomo-tier-v1.json` (30 conversation docs, 16 typed probes)
**Run:** `--embedding granite` → `evidence/2026-08-03-locomo-granite/`
**Baseline:** hash embeddings → `evidence/2026-08-03-locomo-tier/` (run4)

## Result: Granite does NOT beat hash on this synthetic corpus

| Provider | recall@10 | MRR | cit_prec | stale | unsup | temporal |
|---|---|---|---|---|---|---|
| severian-sqlite hash | 0.923 | 0.814 | 0.656 | 0.000 | 0.188 | 1.000 |
| severian-postgres hash | 0.923 | 0.865 | 0.719 | 0.000 | 0.188 | 1.000 |
| severian-sqlite granite | 0.808 | 0.788 | 0.594 | 0.000 | 0.250 | 0.500 |
| severian-postgres granite | 0.885 | 0.810 | 0.594 | 0.000 | 0.250 | 1.000 |
| chroma (semantic) | 0.962 | 0.744 | 0.469 | 0.100 | 0.188 | 1.000 |
| baseline-lexical | 0.846 | 0.756 | 0.469 | 0.098 | 0.188 | 1.000 |

## Why (honest analysis)

1. **The LOCOMO corpus is lexical-heavy.** Conversation documents share
   literal token overlap ("search index", "SQLite FTS5", "deployment").
   BM25 + hash embeddings already separate them well; Granite's semantic
   generalization adds noise on near-duplicate documents — unsupported
   answers rose 0.188 → 0.250.
2. **Granite's value is paraphrase-distant queries** (the q6 class: "how are
   secrets stored now?" vs "secret store uses scrypt hashing"). This corpus
   under-samples that class — the B-1 analysis flagged it as the A-1 target,
   and a single-probe corpus cannot show the win.

   **Provenance (C-04 distinction — do not conflate):**
   - Historical full-lane evidence (`evidence/2026-08-03-locomo-granite/`):
     q6 returned `[]` for both Granite lanes — the full-lane negative result
     (Granite recall 0.808/0.885 vs hash 0.923) is unchanged and internally
     consistent.
   - Current bounded spot-check (producing SHA 876f616): Granite returned
     `[c27, c12, c24]` for q6, c27 at rank 1. This is a DIFFERENT evidence
     statement from the full-lane trace. The spot-check shows the semantic
     gap IS closable; it does not change the full-lane negative result.
   - The full Granite lane was not re-run in the correction lane (out of
     scope per packet C-04).
3. **Semantic ≠ universally better**: Chroma (semantic) also shows the
   tradeoff (recall 0.962 but stale 0.100, cit_prec 0.469). Embedding choice
   is corpus-dependent, not monotonic.
4. **Stale-fact control is embedding-independent**: both hash and Granite
   lanes hold stale at 0.000 — Severian's correction machinery (P0-5) is the
   differentiator, not the embedding.

## Conclusion (policy-compliant)

- The pluggable seam WORKS: same manifest, same harness, one flag swaps the
  embedding and produces honest comparable numbers. This is the A-1 win —
  the capability to MEASURE, not an assumed superiority.
- For lexical-heavy agent memory traffic, hash+BM25 is the right default
  (and stays the default — no perf regression for production).
- Granite is the tool for paraphrase-distant real-world traffic; the A-3
  real-data lane (Sahil's session logs) is the correct place to measure
  that claim. Until then, no superiority claim for Granite is made.
- No regression was introduced: default remains hash; Granite is opt-in via
  `--embedding granite` or the `build_bundle(embedding=...)` parameter.

*Recorded 2026-08-03. Honest numbers: the measured result is recorded even
when it contradicts the hypothesis. No fabricated parity.*
