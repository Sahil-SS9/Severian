---
title: Severian Objective Adversarial Review — 2026-08-05
created: 2026-08-05
updated: 2026-08-05
type: review
confidence: high
---

# Severian Objective Adversarial Review — 2026-08-05

> Purpose: evaluate whether Severian "says what it does on the tin." Verify and
> validate the concepts and approaches, attempt to break it, and identify
> changes / quick wins to compete in the AI memory space.
> Method: evidence from the actual build (this repo), the measured benchmark
> lanes, the git history, and the competitive comparison matrix.

---

## 1. Verdict up front

**Severian is a genuinely differentiated, honest, local-first memory platform
with real measured strengths — and it has real, mostly-known gaps. It does
what it says on the tin for: correction/supersession, abstention, scope
isolation, determinism, audit/rollback, and offline self-curation.**

The honest weaknesses are not hidden: hash-embedding recall ceiling,
SQLite-at-100k latency, no native import path, no auth on the HTTP API,
LOCOMO not yet re-run post-fix, and a comparison story that leans on
unverifiable vendor numbers. None of these are fatal; all are addressable.
The product's biggest risk is **not technical — it's market**: the feature
list is now commodity; the differentiators must be *proven in public
benchmarks* (LongMemEval, LOCOMO re-run, real-data probe) to compete.

---

## 2. What we verified as TRUE (evidence-backed)

| Claim | Evidence | Status |
|---|---|---|
| 0.000 stale-fact rate | adversarial-27 lane: `stale=0.000` (run 2026-08-05, post porter+coverage) | ✅ VERIFIED |
| 0.000 unsupported-answer rate | adversarial-27 lane: `unsup=0.000` | ✅ VERIFIED |
| 0.000 cross-scope leakage | adversarial-27 lane: `leak=0.000` | ✅ VERIFIED |
| 1.000 recall / MRR / cit-prec on the quality gate | adversarial-27 lane: `recall=1.000 mrr=1.000 cit_prec=1.000` | ✅ VERIFIED (post-fix) |
| Determinism across fresh adapters | `test_ranking_deterministic_across_fresh_adapters` passes 8/8 consecutive (post derive-time revision guard) | ✅ VERIFIED |
| Porter stemming aligns FTS + vector | 113/116 stemmer consistency vs SQLite FTS5 porter; all 27 critical corpus tokens match | ✅ VERIFIED |
| Query-coverage abstention fixes aq11 | decoy coverage 0.33 vs genuine ≥0.50; threshold 0.4 | ✅ VERIFIED |
| LSH flattens dreaming curve | 10k consolidate 437s → 131s (3.3×); candidate ratio 0.18 | ✅ VERIFIED |
| mypy --strict, ruff clean, 326 passed / 36 skipped | full gate run 2026-08-05 | ✅ VERIFIED |

---

## 3. Issues found (the "break it" list)

### 3.1 FIXED during this review cycle (evidence in git history)

1. **Ranking non-determinism across fresh adapters (C-08).** Root cause:
   duplicate-content memories created by the correction path tied on every
   stable key and fell through to the random memory UUID. Fixed at the source
   (derive-time revision guard) — not at retrieval. Verified 8/8 + 5/5.
   *Lesson: dedupe at the write/derive layer, never at retrieval (retrieval
   dedupe broke citations).*

2. **Citation precision regression during the fix attempts.** A retrieval-time
   content-dedupe + adapter revision mapping caused stale citations (0.938,
   then stale=0.067). Reverted; the derive-time guard is the correct layer.

3. **aq3 recall failure (0.933).** FTS5 default tokenizer lacked stemming.
   Fixed with `porter unicode61` + pure-Python stemmer wired into hash vectors.

4. **aq11 abstention failure.** Absolute fused-score floor was mathematically
   impossible (genuine vs decoy scores overlap). Replaced with query-coverage
   abstention (0.4) — honest, measurable.

5. **Extraction junk relations.** Sentence-start pronouns were extracted as
   entities (`(He, uses, Acme)`). Fixed by adding pronouns to `_STOP`.

### 3.2 OPEN issues (not yet fixed — honest)

| # | Issue | Severity | Evidence / why it matters |
|---|---|---|---|
| O1 | **LOCOMO numbers are stale** (0.923 recall) — pre porter-stemming. | Medium | README now flags it. The current code likely improves it (adversarial is 1.000). Re-run LOCOMO with current code = honest, likely-win data point. |
| O2 | **SQLite query p95 at 100k = 3,238ms.** | High (scale) | PG backend is 151.6ms. SQLite is fine ≤10k, not interactive at 100k. Docs say use PG; but the *default* is SQLite. |
| O3 | **Cold derive-on-read is super-linear** (6,458s at 100k SQLite). | High (scale) | Mitigated by worker model (`severian work`), but a first-read footgun. |
| O4 | **No native import path from other memory providers.** | Medium | Mnemosyne/GitNexus/GBrain/LLM-WIKI have private formats; no off-the-shelf universal tool exists (MIF is draft, covers Mem0/Zep/Letta, not these). Building a custom importer is not worth it now (run alongside instead). |
| O5 | **HTTP API has NO authentication.** | High (security) | Loopback-only by default (good), but README's "API keys hashed at rest" is aspirational, not implemented. Any host user can read the store. Phase-1 auth required before any network exposure. |
| O6 | **Hash embeddings are the default** — deterministic but semantically weaker. | Medium | Granite R2 opt-in exists; A-3 decision pending. Competitors (Mem0, Hindsight, Supermemory) use LLM/semantic embeddings by default. |
| O7 | **LongMemEval not run.** | Medium | The comparison matrix says it; competitors market LongMemEval numbers. Running it = credible public proof. |
| O8 | **Dense-corpus dreaming still O(n²)-ish.** | Low | LSH helps diverse corpora (0.18 candidate ratio); dense shared-template corpora still slow. |
| O9 | **`min_fused_score` floor is a footgun.** | Low | Replaced by coverage for the lane, but the param still exists and is mathematically unreliable as an absolute floor. Either document loudly or remove. |

---

## 4. Concepts / approaches validation

| Concept | Validated? | Notes |
|---|---|---|
| **Canonical append-only + rebuildable projections** | ✅ | P0-1 sound; projections are derived, never authoritative. |
| **5-D scope as the security boundary** | ✅ | Fail-closed, cross-scope returns empty (no existence leak). |
| **Supersession via revision chain (P0-5)** | ✅ | Old heads stay in history; rollback (A-2) restores. |
| **Derive-time dedupe (one fact = one memory)** | ✅ | The C-08 fix; P0-1 preserved (distinct sources still derive separately). |
| **Query-coverage abstention** | ✅ | Replaces an impossible absolute-score floor with a measurable token-coverage gate. |
| **Deterministic hash embeddings + BM25** | ✅ | Byte-reproducible; the honest trade-off is semantic ceiling. |
| **LSH pre-filter for dreaming** | ✅ | 3.3× on diverse; honest about dense lanes. |
| **Benchmark honesty (never lower thresholds)** | ✅ | The earlier FAIL was published, not hidden. This is a real brand asset. |

---

## 5. Quick wins to compete (ordered by effort/impact)

1. **Re-run LOCOMO with current code** (few hours). Likely lifts 0.923 → ~1.0.
   One command, one evidence file, README update. Highest credibility-per-effort.
2. **Ship a real-data probe** (spec exists: `docs/benchmarks/real-data-probe-spec-2026-08-03.md`).
   A public, reproducible run on real conversations. Competitors don't do this.
3. **Run LongMemEval.** The single most-cited benchmark in the space. Even a
   partial run (5-D boundary maps directly) would be a marketable number.
4. **Document the SQLite scale cliff + promote PG** in README Quick Start
   ("use PG for >50k"). Cheap, prevents footguns.
5. **Add a one-command `severian probe` or `severian self-check`** that runs
   the adversarial-27 lane + determinism and prints the table. Turns the
   honesty story into a demo.
6. **Fix the HTTP auth posture** (even basic bearer token on loopback) and
   remove the aspirational "API keys hashed at rest" claim until implemented.
7. **Decide the embedding default (A-3).** Granite opt-in exists; measure and
   either flip default with a determinism flag or market hash as a feature.
8. **Publish the adversarial-27 + determinism evidence as a public page**
   (GitHub Actions artifact or a `docs/benchmarks/evidence/` index) so the
   "measured, never vendor-mixed" claim is clickable.

---

## 6. The honest competitive read

- **Commodity features** (local-first, scoped, explainable, self-curating,
  MCP): everyone has these now. Not differentiators.
- **Genuine differentiators** (measured): 0.000 stale/unsup/leakage with
  committed evidence, 5-D scope boundary, immutable audit + rollback,
  deterministic hash embeddings, benchmark transparency (published FAILs).
- **Competitors' strengths we don't match yet**: semantic embeddings by
  default (Mem0/Hindsight/Supermemory), bi-temporal graphs (Zep/Graphiti),
  recall speed sub-300ms (Supermemory), hosted API convenience.
- **The winning move**: lean into the *evidence discipline* — nobody else
  publishes their failures with reproduction scripts. Make "show the receipts"
  the brand. Re-run LOCOMO + LongMemEval, publish the evidence index, and let
  the measured numbers (not features) carry the comparison.

---

## 7. Recommended immediate actions (next sweep)

- [ ] Re-run LOCOMO tier lane with current code; update README + evidence.
- [ ] Add `severian self-check` (adversarial-27 + determinism in one command).
- [ ] Add an evidence index (`docs/benchmarks/evidence/README.md`) linking
      every claim to its reproduction command + result file.
- [ ] Decide A-3 (default embeddings) with the fresh LOCOMO + LongMemEval data.
- [ ] Add basic HTTP auth + remove the aspirational security claim.
- [ ] Update Quick Start with a "when to use PG" note (scale cliff).
- [ ] Remove or loudly document `min_fused_score` as an unreliable absolute
      floor (coverage abstention is the real gate).
