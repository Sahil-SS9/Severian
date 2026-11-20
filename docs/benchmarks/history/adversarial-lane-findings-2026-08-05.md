# Adversarial-27 lane (A-3 option 3) — findings, 2026-08-05

**Status:** LANE BUILT + RUN (hash and Granite). A-3 real-data lane remains
SPEC-only pending corpus approval (session logs need explicit PII-scrubbed
approval; no response received, so option 3 — adversarial-synthetic — was
the safe default).

**Manifest:** `manifest-adversarial-v1.json` (27 docs, 16 queries).
Generator: `scripts/gen_adversarial_manifest.py`.

## What the lane adds over manifest-v1

- typo / punctuation noise (aq2)
- paraphrase distance (aq3) — the semantic-embedding zone
- contradiction chains (aq4: a10 → a11 → a12)
- decoys with high lexical overlap (aq5/aq6/aq12)
- cross-scope decoys (aq7: a16 in t2 must never surface)
- multi-source (aq8)
- long-tail vocabulary (aq9)
- near-duplicate pair (aq10: a22 canonical, a23 superseded)
- negative / abstention (aq11)
- temporal past/present pairs (aq13-aq16)

## Results (2026-08-05) — RECONCILED at current HEAD (9d7c989, feat/ai-memory-concepts)

> **EVID-03 reconciliation:** the numbers below were re-run at commit
> `9d7c989` (2026-08-05T20:0x) via `scripts/run_adversarial_lane.py`.
> The earlier rows in this document (0.933 recall / 0.875 cit_prec /
> 0.022 stale) predate the determinism fix (derive-time revision guard +
> superseded-first corpus ordering) and are superseded by the rows
> below. Gate: **passed** (`gate_failures: {}`).

### Hash embeddings (Severian default, raw capability mode)

| Provider | recall@10 | MRR | cit_prec | stale | temporal | leak | unsup |
|---|---|---|---|---|---|---|---|
| **Severian SQLite (hash)** | **1.000** | **1.000** | **1.000** | **0.000** | 1.000 | 0.000 | **0.000** |
| Baseline (lexical) | 0.933 | 0.718 | 0.562 | 0.173 | 0.750 | 0.000 | 0.062 |

The reconciled lane meets the full release target (recall 1.000, MRR
1.000, stale 0.000, cit_prec 1.000). The derive-time revision guard
(Turn 1) closed the residual stale path and the superseded-first
ordering closed the cit_prec gap; abstention (aq11) now returns no
hits because the query-coverage abstention (min_coverage=0.4) rejects
the weak decoy overlap.

### Granite R2 embeddings (semantic, opt-in)

| Provider | recall@10 | MRR | cit_prec | stale | temporal | leak | unsup |
|---|---|---|---|---|---|---|---|
| **Severian SQLite (Granite)** | 1.000 | 0.967 | 0.875 | 0.021 | 1.000 | 0.000 | 0.062 |

> **Reconciliation note:** the Granite row above is the pre-fix
> measurement from the original 2026-08-05 run (before the derive-time
> revision guard). At current HEAD, `run_adversarial_granite.py`
> cannot re-measure with real Granite embeddings because
> `sentence_transformers` is not installed in this session's
> environment (falls back to hash with a different adapter
> configuration, which is NOT a valid Granite result). The Granite
> lane remains an opt-in semantic embedding; its release-critical
> status is NOT blocking (Chroma/Granite are never release-blocking).
> Re-run requires installing the optional embedding dependency.

## Findings

1. **Granite fixes paraphrase RECALL: 0.933 → 1.000.** On aq3 the expected
   doc (a08) enters top-10 where hash missed it. This is direct evidence
   for the A-3 semantic-embedding decision: Granite closes the recall gap
   on paraphrase-distance queries.

2. **Granite does NOT fix aq3 rank-1 (cit_prec unchanged 0.875).** a09
   ("Requests are checked for valid credentials at the entry gate") still
   outranks a08 ("Every incoming request is authenticated before it
   reaches the handler") for "How is authentication enforced on inbound
   requests?". Both are semantically close; the embedding ranks the
   lexical-heavy sibling first. cit_prec 1.0 needs more than embeddings.

3. **Abstention (aq11) is unsolved by either embedding.** The floor keeps
   any hit with lexical_score > 0.0 (LOCOMO q7 design — genuine lexical
   evidence must survive). The decoy a15 ("headcount target for Q3 twelve
   new engineers") shares "new/the/is" with "What is the salary of the
   new CFO?" and survives. Granite returned a15/a14/a19 — semantic
   proximity does not equal abstention. This is the documented boundary:
   abstention = "no strong match", not "semantic comprehension".

4. **Supersession machinery exercised correctly after fixes.** With
   superseded-first corpus ordering + the derive superseded-observation
   guard (Turn 1), stale facts no longer surface: aq4 (a12 final region),
   aq10 (a22 canonical), aq15 (a04 current password manager), aq13/aq14
   (current/old API) all pass. Residual stale 0.022 is one probe where a
   superseded observation's history entry appears below the active head —
   the temporal fallback, not active search.

5. **Leakage stays 0.000** even with the cross-scope decoy a16 (t2) —
   the 5-D boundary holds under adversarial pressure.

## Decisions recorded

- aq10 expected evidence stays single (a22 canonical). The runner's
  cit_prec is AND-semantics over expected evidence; OR-semantics would
  need a runner change. Dedup/consolidation is the dreaming service's
  job, exercised in `tests/application/test_dreaming.py`.
- Abstention floor stays OPT-IN and unchanged (default None = raw
  capability). The adversarial lane documents the boundary; it does not
  change the floor design.

## Next steps for the A-3 programme

1. A-3 real-data lane still needs Sahil's explicit corpus approval
   (session logs, PII-scrubbed) or a licence-cleared public dataset.
2. If cit_prec 1.0 on adversarial is required: investigate a
   query-kind-aware reranker or an abstention-on-semantic-distance rule
   (beyond lexical overlap) — both are new features, not bug fixes.
