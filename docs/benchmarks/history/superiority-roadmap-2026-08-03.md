# Severian — Superiority Roadmap (research interrogation output)

**Date:** 2026-08-03
**Source prompt:** `docs/benchmarks/handover-superiority-2026-08-03.md` Part 2 (executed inline)
**Inputs:** `results.md` (§1–§8), `manifest-v1.json`, `evidence/2026-08-03/*.json`,
`src/severian/benchmarks/adapters/*.py`, wiki concepts + comparisons (citations inline).
**Rule honoured:** every claim cites a measured evidence file OR a wiki page + paper ID.
Vendor numbers stay vendor-reported (results.md §5) — never mixed into independent claims.

---

## 1. Tier-by-tier gap analysis

### T1 — Ingest / write path
| Position | Evidence |
|---|---|
| Severian: 248 / 254 / 206 docs/s (sqlite), 170 / 156 / 160 (PG) at 1k/10k/100k; serial per-doc adapter ingest; durable (SQLite WAL / PG txn, FTS+vector projections) | scale-1k-10k.json, scale-100k.json |
| Chroma: ~4 docs/s per-doc path (0.31 s/doc → 100k skipped, ~9 h); batch ingest exists but unexercised | scale-1k-10k.json, scale-100k.json skipped_lanes |
| Baseline: ~300k docs/s in-memory, zero durability | same files |
| **Delta:** batch/vectorised ingest path (target ≥1k docs/s), transactional batch boundaries | wiki: memtxn-memory-transactions (2607.27834); mandol 4.8× insertion speedup (2606.29778) |

### T2 — Representation
| Position | Evidence |
|---|---|
| Severian: immutable observations + derived memories; hash+BM25, 256-dim hash vectors; FTS + vector projections; no semantic embeddings | adapters/severian.py; results.md §6 |
| Best-in-class: Granite R2 97M — 60.3 MTEB retrieval, 384-dim, 32K ctx, Apache-2.0, CPU (ONNX/OpenVINO); Mandol unified KV+vector+graph; Danus fact graph; MRAgent cue-tag-content graph | wiki: ibm-granite-embedding-r2; mandol-agglomerative-memory (2606.29778); danus-fact-graph-memory (2607.06447); mragent (2606.06036) |
| **Delta:** pluggable embedding provider (Granite R2 as local-first default); embedding choice alone shifts accuracy +6.2pp (p=0.004) | wiki: memdelta-controlled-memory-evaluation (2606.29914) |

### T3 — Extraction
| Position | Evidence |
|---|---|
| Severian: deterministic derive pass (hash-fact per observation), localized per-scope async worker in production | adapters/severian.py `_LocalHashGateway`; results.md §4 |
| Best-in-class: attention-guided targeted updates (AGMR), LLM-judge extraction (MemRefine), selective injection (proactive memory agent +8.3pp Terminal-Bench 2.0) | wiki: attention-guided-memory-refinement (2607.17621); memrefine (2606.13177); proactive-memory-agent (2607.08716) |
| **Delta:** optional pluggable extractor with determinism guarantee (default stays hash); salience/utility weighting of derived memories | — |

### T4 — Retrieval + routing
| Position | Evidence |
|---|---|
| Severian: fused FTS+vector score, relevance floor (abstention), 5-D scope isolation; recall@10 / MRR / nDCG / cit-prec all 1.000, leak 0.000, unsup 0.000 | results.md §1 |
| Best-in-class: BM25 + lightweight neural reranker hybrid (paper's explicit recommendation); Mandol adaptive routing + denoising, LLM-free retrieval; LRE load-bearing eviction | wiki: bm25-wins-at-scale-rag (2607.26497); mandol (2606.29778); learned-relevance-eviction (2606.20954) |
| **Delta:** reranker tier over BM25 candidates; query-kind routing; embeddings close the paraphrase-distant gap (q3-class queries at real-world scale) | wiki: bm25-wins-at-scale-rag; keyword-vs-embeddings-brain-search (comparison) |

### T5 — Maintenance / update
| Position | Evidence |
|---|---|
| Severian: supersession via revise_observation (superseded_by_id, tombstoning), stale 0.000 vs Chroma 0.179; crash-safe worker, idempotent drain; localized maintenance already implemented (derive=False worker) | results.md §1, §6; adapters/severian.py |
| Best-in-class: A-TMA three-level ghost-memory fix (Severian already implements all three levels — bank tags, retrieval filtering, answer-time recency resolution); ChronoMem versioning + semantic rollback (missing from ALL major systems); MemRefine budgeted compression; budget-dependent operator selection (+48% LongMemEval under tight budgets) | wiki: atma-ghost-memory (2607.01935); chronomem-version-control (2607.27773); memrefine (2606.13177); budget-dependent-memory (2607.17545) |
| **Delta:** version inspection + rollback; retention policy tier (LLM-judge merge/delete with rule-based fallback) | — |

### T6 — Conversation recall
| Position | Evidence |
|---|---|
| Severian: session_id first-class in 5-D scope; observations+derived memories are the raw material; NO agent-loop benchmark run (results.md §8), NO episodic summary path — no measured conversation-recall number | results.md §8; handover (d) |
| Best-in-class: LOCOMO / EvoArena / LongMemEval tiers; Mandol best on LoCoMo + LongMemEval; Oracle 93.8% LongMemEval, 10.7× fewer tokens; EvoMem patch memory +4.8% LoCoMo | wiki: evoarena (2606.13681); mandol (2606.29778); oracle-agent-memory (2607.13157) |
| **Delta:** LOCOMO-style tier on own harness; episodic summarization (MemRefine/EvoMem pattern) so long sessions retrieve as episodes, not 10k raw observations | — |

### T7 — Operations / security
| Position | Evidence |
|---|---|
| Severian: local-first, deterministic, offline, CVE-free; documented PG deployment; 5-D boundary; 254 tests green; lease/reap, atomic abort | results.md §5 security note, §6; strategy.md §2 |
| Chroma 1.5.9: unpatched critical pre-auth RCE CVE-2026-45829 (CVSS 10.0) — cannot be network-exposed | results.md §5 |
| Best-in-class patterns: ContextNest context governance (approval, version identity, integrity, attribution, point-in-time reconstruction); TMA-NM origin-bound authority; selection-integrity reallocatability audit; MemLeak IPG deletion-affordance audit; MemTxn source verification | wiki: contextnest-verifiable-context-governance (2607.02116); non-malleable-memory-authority (2606.24322); selection-integrity-graph-memory (2606.12290); memleak-information-leaks (2606.29788); memtxn-memory-transactions (2607.27834) |
| **Delta:** version inspection (ChronoMem), retrieval-time provenance/integrity chain (ContextNest), deletion-affordance audit (MemLeak IPG), source-verification on write path (MemTxn) | — |

---

## 2. Benchmarks to acquire (ranked)

| # | Benchmark | Measures | Runtime | Feasible with adapter protocol | Competitor claims it addresses |
|---|---|---|---|---|---|
| 1 | **LOCOMO-tier conversation recall** (own harness, typed probes: fact/multi-hop/temporal/adversarial — manifest kinds already encode the taxonomy) | conversation-level recall, temporal correctness, abstention | hours (retrieval-only scoring keeps determinism; LLM answer scoring optional) | YES — new corpus + query tier, existing runner | Mem0 ECAI 2025 LOCOMO claims, Hindsight, Honcho, SuperMemory |
| 2 | **MEMPROBE-style recovery audit** | post-interaction user-state reconstruction (1,550 targets / 50 users) | 1–2 h, no LLM needed | YES — new corpus + probe set | all vendors' "memory works" narratives |
| 3 | **LongMemEval tier** (public dataset) | long-conversation QA; consolidation impact | half-day | YES after corpus import | Oracle 93.8%, Mandol best |
| 4 | **StreamMemBench-style streaming** (future-oriented: observation → application) | trajectory coverage, future-assistance recall | half-day | YES | Hindsight passive-capture claims |
| 5 | **Scale lane extension: batch-ingest comparison** | Severian batch vs Chroma batch vs current serial | ~1 h | YES — add batch method to adapter protocol | Chroma vectorized-batch claim |
| 6 | **EvoArena-style dynamic env** (EvoMem patch memory) | adaptation under changing rules | days (agent loop) | partial — needs agent loop tier | Mem0 agent-loop claims |
| 7 | BEAM 10M-token | 128K–10M token scale | days + dataset + LLM API | partial | future; document only |

Prioritised per handover (d): #1 and #2 first — they produce the honest conversation-recall number Severian currently lacks.

---

## 3. Techniques to adopt, ranked by leverage

1. **Pluggable semantic embeddings — Granite R2 97M.** Fixes: W3 (no semantic retrieval). Source: ibm-granite-embedding-r2; memdelta (2606.29914). Neutralises: Chroma default embedding + cloud embedding narrative. Seam: provider adapter interface + vector projection (dimension configurable 256→384; keep hash fallback for determinism). Effort: M. Impact: semantic recall on paraphrase-distant queries; honest vendor parity on embedding-dependent dimensions.
2. **BM25 + reranker hybrid retrieval.** Fixes: precision at scale. Source: bm25-wins-at-scale-rag (2607.26497). Neutralises: dense-only competitors. Seam: retrieval fusion (fused_score). Effort: M. Impact: p95 precision gains without sacrificing the 1.000 baseline.
3. **Version control + semantic rollback (ChronoMem layer).** Fixes: W1/W2 risk surface (cold rebuild without recovery); ops differentiator — ALL major systems are forward-only. Source: chronomem-version-control (2607.27773); ContextNest version identity (2607.02116). Seam: observation supersession → add version inspection + rollback. Effort: M. Impact: competitive moat, not parity.
4. **Episodic summarization with budget (MemRefine + EvoMem + budget-dependent operator).** Fixes: W4/W5 (conversation recall at scale; 10k raw observations → episodes). Source: memrefine (2606.13177); evoarena (2606.13681); budget-dependent-memory (2607.17545). Neutralises: Hindsight/Honcho conversation narrative. Seam: derive worker + consolidation pass. Effort: L. Impact: conversation recall with bounded storage.
5. **Batch ingest path.** Fixes: W4 (throughput). Source: measured delta vs Chroma batch; memtxn (2607.27834) for atomic batch boundaries. Seam: adapter ingest + service. Effort: S–M. Impact: ≥1k docs/s, honest closure of the batch gap.
6. **Context governance / provenance chain on retrieval (ContextNest).** Fixes: provenance/version/integrity gaps; security moat. Source: contextnest (2607.02116); memtxn source verification (2607.27834). Seam: projection/retrieval layer. Effort: M. Impact: verifiable context — pairs with supersession.
7. **MEMPROBE-style recovery metric as a standing health gate.** Fixes: no memory-health metric (task success ≠ memory recovery). Source: memprobe-memory-recovery-benchmark (2606.24595). Seam: benchmark suite. Effort: S. Impact: honest, cheap, continuous memory-quality signal.

Deferred / watch: Mandol agglomerative store (2606.29778) — evaluate when multi-gateway concurrency bites; proactive memory sidecar (2607.08716) — agent-loop product tier, not storage tier; TF-Engram SSD tier (2607.07388) — when corpus exceeds memory.

---

## 4. What NOT to copy

- **Cloud-only embedding dependence** (Chroma default downloads models; Mem0 hosted): breaks local-first determinism. Keep hash fallback + CPU-runnable embeddings (Granite R2 ONNX/OpenVINO).
- **Non-deterministic defaults** (Chroma embedding varies across versions; Ollama embedding gen adds 2–5 s): keyword/FTS stays the default path — keyword-vs-embeddings-brain-search comparison.
- **Per-doc write paths** (Chroma 0.31 s/doc → 9 h at 100k): batch, never per-doc, as the primary write path.
- **Naive overwrite** (A-TMA ghost memory): keep superseded facts tagged as historical — never hard-delete current-state history.
- **Attention/recency-only eviction** (rate-distortion 2607.08032: fails identically at every layer): prefer load-bearing signals (LRE 2606.20954).
- **Passive-storage-only** (ATMem 2606.31612): keep the active worker/derive path; storage-only retrieval is the competitor baseline.
- **Cross-scope shared memory without the 5-D boundary** (HyphaeDB gossip 2606.28781): any propagation must respect scope or it becomes leakage.
- **LLM in the control plane** (control-plane placement 2606.15903: hallucination risk): keep the deterministic control plane; optional LLM in recall plane only.
- **Graph memory without selection-integrity audit** (2606.12290 reallocatability): if a Danus-style fact graph is ever added, audit the selector first.
- **Unbounded retention** (MemRefine 2606.13177: surface similarity ≠ factual value): budgeted compression before unbounded growth.

---

## 5. The superiority claim set

**Can claim now (measured, independent):**
- Zero stale-fact leakage on corrections: stale_fact_rate 0.000 vs Chroma 0.179 (gate, results.md §1).
- Zero cross-scope leakage across the 5-D boundary: 0.000 both backends (§1).
- Zero fabrication on unanswerable queries: unsupported 0.000 vs Chroma 0.062 (§1).
- Perfect retrieval precision on manifest corpus: recall@10 / MRR / nDCG 1.000 (§1).
- Network-safe by default; principal open-source competitor carries unpatched CVE-2026-45829 (§5).
- Deterministic, offline, reproducible benchmarks (manifest SHA + seed + commit pinned, §7).

**Can claim after benchmark X:**
- Conversation-recall number on a LOCOMO-tier run (addresses Mem0/Hindsight/Honcho/SuperMemory directly).
- LongMemEval score vs Oracle 93.8% / Mandol best (after import).
- Future-oriented memory score (StreamMemBench-style) vs Hindsight passive capture.

**Can claim after implementation Y:**
- Semantic retrieval parity: MTEB 60.3-grade retrieval locally, offline (Granite R2 embeddings).
- Only memory platform with version control + semantic rollback (ChronoMem layer).
- Bounded memory without recall loss: budgeted episodic compression (MemRefine tier).
- Batch ingest ≥1k docs/s with atomic transaction boundaries (batch path + MemTxn pattern).

---

## 6. Executive summary (10 lines)

1. The research validates Severian's architecture: BM25 base, supersession, localized maintenance are all independently supported — the weaknesses are roadmap items, not design flaws.
2. Three highest-leverage acquisitions: (1) pluggable semantic embeddings (Granite R2 97M, Apache-2.0, CPU), (2) BM25 + reranker hybrid, (3) ChronoMem-style version control + semantic rollback.
3. Each maps to an existing seam (provider adapter, fusion layer, supersession) — none requires redesign.
4. The single most position-changing benchmark: a LOCOMO-tier conversation-recall run on our own harness — it converts the biggest honest gap (no conversation number) into the strongest vendor-parity claim.
5. Second benchmark: MEMPROBE-style recovery audit — cheap, deterministic, and it exposes the "memory ≠ task success" truth competitors hide.
6. Conversation recollection is a requirement (Sahil's (d)): benchmark tier first, episodic summarization second, claim only what we measure.
7. Competitor claims are legitimate targets but never our numbers — same manifest, same harness, vendor-reported stays labelled (Sahil's (b)).
8. Chroma's CVE and 9-hour 100k ingest are material, measured wins we can state today.
9. Traps to refuse: cloud embeddings, per-doc writes, LLM-in-control-plane, cross-scope propagation, unbounded retention.
10. After the roadmap: Severian claims correctness (now), conversation recall (after LOCOMO-tier), and versioned, bounded, verifiable memory (after Y) — all defensible on our own evidence.
