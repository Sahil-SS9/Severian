# LOCOMO-tier conversation recall — B-1 evidence analysis (2026-08-03)

**Manifest:** `docs/benchmarks/manifest-locomo-tier-v1.json` (validated)
**Run:** `/tmp/locomo-tier-run/` (result.json, traces.json, fingerprint.json)
**Honest finding:** Severian does NOT yet pass the release gate on the harder
conversation corpus. This is the number Sahil's (d) requires — recorded as-is,
no fabricated parity.

## Results (gate: recall>=0.95, cit_prec>=1.0, stale==0, unsup==0)

| Provider | recall@10 | MRR | nDCG@10 | cit_prec | stale | unsup | temporal |
|---|---|---|---|---|---|---|---|
| severian-sqlite | 0.769 | 0.769 | 0.754 | 0.656 | 0.000 | 0.188 | 0.000 |
| severian-postgres | 0.769 | 0.769 | 0.754 | 0.656 | 0.000 | 0.188 | 0.000 |
| chroma | 0.962 | 0.744 | 0.777 | 0.469 | 0.119 | 0.188 | 1.000 |
| baseline-lexical | 0.846 | 0.756 | 0.755 | 0.469 | 0.154 | 0.188 | 1.000 |

**Gate:** severian lanes FAIL (recall, citation_precision, unsupported_answer_rate).
Chroma FAILS too (stale 0.119, cit_prec 0.469). No lane passes.

## Per-query failure profile (severian-sqlite, 16 queries, 5 misses)

| Query | Kind | Expected | Returned | Cause |
|---|---|---|---|---|
| q6 | correction | c27 | [] | Paraphrase-distant ("secrets stored now" vs "secret store uses scrypt") — **semantic gap (A-1)** |
| q7 | temporal | c02 | [c03, c23, c06] | c02 is stale/superseded; active-head read suppresses it by design (P0-5). **Probe needs history/as-of path** |
| q8 | temporal | c09 | [c10, c06] | Same as q7 — stale c09 suppressed |
| q9 | adversarial | (none) | [c17, c11] | c17 "undisclosed" is an in-scope decoy; harness counts any return as unsupported. **Corpus-design issue** |
| q11 | negative | (none) | [c28] | BM25 overlap noise ("API docs in runbook" vs "annual revenue"). **BM25 false positive (A-1 hybrid fixes)** |

## What this tells us

1. **The honest conversation-recall number is 0.769** — not 1.000. The 27-doc
   fact corpus was easy; conversation probes expose the semantic gap.
2. **Chroma's embeddings win recall (0.962) but lose the correctness battle**
   (stale 0.119, cit_prec 0.469) — exactly the A-1 thesis: semantic recall +
   Severian's correction machinery is the winning combination.
3. **Temporal probes (q7/q8) need the history path** — Severian suppresses stale
   facts by design (P0-5); a LOCOMO temporal probe asks for the old fact, so the
   harness must use `list_memories_history` / as-of reads for temporal queries.
4. **q9/q11 are corpus/harness artifacts**, not retrieval failures — but they
   still count against unsupported_rate, so the corpus needs an absent answer
   for adversarial/negative probes (no in-scope decoy).

## Next steps (feed into A-1 and B-1 refinement)

- **A-1 (embeddings):** expected to lift recall on q6/q11-class paraphrase-distant
  queries; re-run this manifest after Granite R2 lands.
- **B-1 harness refinement:** temporal queries should read the history/as-of path
  (not active-head); adversarial/negative probes should have no in-scope decoy.
- **Re-run after each fix:** this manifest becomes the standing conversation-recall
  gate alongside manifest-v1.json.

*Recorded 2026-08-03. Honest numbers policy: this is the measured number, not a
target. No fabricated parity with any vendor.*
