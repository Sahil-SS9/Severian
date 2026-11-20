# Severian embedding support tiers — 2026-08-09 (SUPERSEDED)

> **SUPERSEDED — INVALID FOR MODEL SELECTION** (2026-08-10 round-two remediation)
>
> This document was published with the 2026-08-09 zero-debt candidate
> (SHA `c92769c`, branch `zero-debt-embedding-perf`, uncommitted state) on
> the old real-data corpus (`de09ea4e…`) under the OLD gate semantics. It is
> HISTORICAL ONLY: the Granite selections below are not valid round-two
> evidence, the old suite counts (580/41, 667/11, 610/13) describe the
> pre-remediation tree, and **NO MODEL SELECTED** is the current status
> (as of 2026-08-10). Round-two selection requires the frozen corpus
> (`docs/benchmarks/corpus/quality-corpus-v1.json`) and the corrected
> release gate (`docs/benchmarks/embedding-gate-freeze-2026-08-10.md`).
> Replacement evidence will be published in a new dated directory under
> `docs/benchmarks/evidence/` after the round-two bake-off.

Status (historical): published with the zero-debt embedding performance
candidate (SHA `c92769c`, branch `zero-debt-embedding-perf`).
Evidence: `docs/benchmarks/evidence/2026-08-09-zero-debt/`.

## Selections

| Tier | Selection | Model | Dim | Recall@10 | MRR | nDCG@10 | p95 ms | Notes |
|---|---|---|---|---|---|---|---|---|
| **Default** | `hash` (deterministic) | HashEmbedding(dimension=256) | 256 | baseline lane | — | — | ~1 ms | Deterministic fallback SPACE; never silently substituted (resolver policy). Correct for offline/deterministic retrieval and empty-index stamping. |
| **High (semantic)** | `granite` → GraniteR2Embedding | ibm-granite/granite-embedding-311m-multilingual-r2 | 768 | 0.9915 | 0.9066 | 0.9274 | 146.3 | Best measured semantic recall on the real-data corpus; 768-dim. |
| **Lite (semantic)** | `granite` lite option | ibm-granite/granite-embedding-small-english-r2 | 384 | 0.9915 | 0.9149 | 0.9337 | 149.9 | Same recall, smaller dim (1536 bytes/memory vs 3072); English-tuned. |

## Selection rationale (evidence-led)

1. **Default stays deterministic (`hash`)** — the resolver must never swap
   semantic vectors into a hash-stamped space (and vice versa). Space identity
   is stamped and verified (embedding-space identity metadata); opening an
   index in the wrong space raises `EmbeddingSpaceMismatchError`.
2. **Semantic tier = Granite R2** — measured on the redacted real-data corpus
   (`de09ea4e…`, zero PII post-scan): 311m hits recall@10 0.9915 / MRR 0.9066;
   small-english-r2 matches recall with a smaller footprint. Both run in
   `sentence-transformers` (onnxruntime optional; degrades to hash without
   optional extras — no silent substitution).
3. **Rejected lanes (this packet)** — granite-107m, granite-97m,
   gemma-300m, qwen-0.6b, bekko-a8m have no quality run recorded in the
   2026-08-09 packet and are not claimed.
4. **Latency trade-off is explicit** — semantic p95 ~146-150 ms vs hash ~1 ms
   on the scale corpus. High/lite are opt-in via `SEVERIAN_EMBEDDING=granite`;
   the default space remains deterministic hash.

## Support claims allowed by this evidence

- Deterministic default: YES (hash lane, space identity enforced).
- Semantic recall on real data: YES (granite-311m / small-english-r2,
  recall@10 0.9915 on corpus sha `de09ea4e…`).
- Scale 1K/10K with complete warmup: YES (sqlite 245.8/243.7 docs/s,
  postgres 151.0/148.5 docs/s).
- 100K current-SHA claim: **NOT YET** (partial warmup on `c92769c`;
  complete-warmup 100K on this SHA still outstanding).
- "Best embedding model": NO — only the two granite R2 lanes are claimed;
  no cross-vendor best claim is made from this packet.
