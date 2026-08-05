# Severian vs Leading Memory Providers — Benchmarking Comparison Matrix

**Date:** 2026-08-05
**Scope:** manifest-v1 + LOCOMO-tier + adversarial-27 + scale lanes (all
Severian numbers measured on our own harness). Vendor numbers are
LABELLED vendor-reported — never mixed into our measured tables.

---

## 0. How to read this matrix (honest-numbers policy)

- **MEASURED** = Severian ran the lane ourselves, same manifest, same
  harness, evidence files cited. These are our numbers, reproducible.
- **VENDOR** = the provider's own published claim (source + date cited).
  Industry experience (Maximem reproduction, 2026-05-27: Mem0's
  LongMemEval 94.4 → 73.8 under a different harness) shows vendor
  benchmark numbers do not survive reproduction. Treat as directional.
- **UNVERIFIABLE** = a claim we have not measured and cannot reproduce
  with the vendor's conditions. We never claim parity on those.
- Benchmarks measure different things: LongMemEval (500 Qs, 5
  dimensions: extraction, multi-session, temporal, knowledge-update,
  abstention), LoCoMo (multi-session conversations), ConvoMem,
  BEAM-1M/10M. A single "score" hides the per-dimension shape.

---

## 1. Measured on OUR harness (manifest-v1, SQLite lane, 2026-08-05)

Evidence: `docs/benchmarks/evidence/2026-08-04/result.json` +
Turn-1 focused lane (post-fix). Deterministic: two clean runs are
byte-identical.

| Metric | Severian (hash) | ChromaDB 1.5.9 | Source |
|---|---|---|---|
| recall@10 | **1.000** | 1.000 | results.md §1 |
| citation_precision | **1.000** | 0.812 | results.md §1 |
| stale_fact_rate | **0.000** | 0.179 | results.md §1 |
| cross_scope_leakage | **0.000** | 0.000 | results.md §1 |
| unsupported_answer_rate | **0.000** | 0.062 | results.md §1 |
| temporal_accuracy | **1.000** | 1.000 | results.md §1 |

Adversarial-27 (2026-08-05): Severian recall 0.933 / cit_prec 0.875
(hash), recall 1.000 (Granite opt-in). Granite closes paraphrase
RECALL but not rank-1; abstention on lexical-overlap decoys is the
documented boundary (see `adversarial-lane-findings-2026-08-05.md`).

## 2. Capability matrix — Severian vs the field (2026-08-05)

Legend: ✅ native · 🟡 partial / opt-in / self-hosted variant · ❌ absent · ? unknown/unverified

| Capability | **Severian** | Mnemosyne | Honcho | Hindsight | Supermemory | Mem0 | Zep/Graphiti | Letta | ChromaDB | Cognee |
|---|---|---|---|---|---|---|---|---|---|---|
| **Local-first / offline (no API)** | ✅ default | ✅ (Hermes native) | ❌ managed | ✅ (MIT) | ✅ self-hostable | 🟡 self-host | 🟡 self-host | ✅ | ✅ | 🟡 |
| **Correction / supersession (stale = 0.000)** | ✅ measured | 🟡 | 🟡 | 🟡 | ✅ (resolves contradictions) | 🟡 | 🟡 bi-temporal | 🟡 self-editing | ❌ | ❌ |
| **Immutable audit trail + rollback** | ✅ append-only + rollback (A-2) | ? | ❌ | ❌ | ? | ❌ | 🟡 | ❌ | ❌ | ❌ |
| **Scope isolation (5-D)** | ✅ 0.000 leakage | profile-scoped | multi-tenant (workspace/peer/session) | bank-scoped | user-scoped | 4-dim | graph-scoped | block-scoped | namespaces | shared graph |
| **Abstention (unknown → nothing)** | ✅ measured | 🟡 | ❌ | ? | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Deterministic embeddings / offline** | ✅ hash default | n/a | ❌ (LLM deriver) | ❌ (LLM) | ❌ (LLM) | ❌ (LLM) | ❌ (LLM+graph) | ❌ (LLM) | ❌ (model) | ❌ (LLM) |
| **Temporal reasoning** | ✅ 1.000 measured | ? | 🟡 | ✅ (belief nets) | ✅ (temporal tracking) | 🟡 | ✅ bi-temporal (best in class) | 🟡 | ❌ | 🟡 |
| **Knowledge-update handling** | ✅ supersede chain | ? | 🟡 | ✅ | ✅ | 🟡 | ✅ | 🟡 | ❌ | 🟡 |
| **Multi-session / episodic** | ✅ episode jobs | session-based | ✅ (sessions + summaries) | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 🟡 |
| **Graph retrieval (entity/relation)** | ✅ BFS multi-hop | ? | 🟡 (peers) | ✅ (mental models) | ✅ (memory graph) | 🟡 | ✅ SOTA | ❌ | ❌ | ✅ |
| **Self-curation / dreaming** | ✅ worker jobs | ✅ (dreaming) | 🟡 (deriver) | ✅ (reflect) | 🟡 | ❌ | ❌ | 🟡 | ❌ | ❌ |
| **CVE-free network-safe core (2026-08)** | ✅ | ✅ | n/a | ✅ | n/a | n/a | n/a | n/a | ❌ CVE-2026-45829 | n/a |
| **Benchmark transparency** | ✅ open harness + evidence | n/a | ❌ | ✅ arXiv paper | ✅ MemoryBench open | 🟡 vendor numbers vary | 🟡 paper | 🟡 vendor | ❌ | ❌ |

Notes on the field (sources, 2026-08-05):
- **Mnemosyne**: Hermes's native memory provider; Hermes-ecosystem, not a
  standalone platform. Different category — listed for completeness.
- **Honcho**: managed service; peer-centric, async LLM deriver/summarizer,
  dialectic reasoning (honcho.dev docs). No offline mode.
- **Hindsight**: structured-belief memory (4 networks), LongMemEval leader
  (91.4–94.6%, Virginia Tech arXiv, vendor + independently-validated
  claim); MIT, local. Most credible vendor benchmark in the field.
- **Supermemory**: #1 LongMemEval/LoCoMo/ConvoMem (self-reported
  81.6–85.4% LongMemEval); sub-300ms recall; runnable fully local;
  MemoryBench open eval platform (supermemory.ai / GitHub).
- **Mem0**: most-adopted (51K stars, $24M raised); vendor LongMemEval
  93.4–94.4 / LoCoMo 92.5 at <7K tokens/retrieval (mem0.ai blog
  2026-04); Maximem reproduction 2026-05-27: 94.4 → 73.8 under their
  harness — vendor numbers move with the harness.
- **Zep/Graphiti**: temporal knowledge graphs, up to +18.5% LongMemEval
  accuracy (vendor paper); needs a graph DB (operational weight).
- **Letta**: stateful agent framework, self-editing memory, ~74% LoCoMo
  (vendor framing, agent-autonomy conditions).
- **Cognee**: shared-graph inference, no published benchmarks.
- **ChromaDB**: vector store only — no correction, no scope semantics,
  no abstention, no temporal (measured lane above).

## 3. Where Severian wins (measured, not claimed)

1. **Correctness under correction** — stale_fact_rate 0.000 measured on
   manifest-v1 AND LOCOMO-tier (vs Chroma 0.144–0.179). Supermemory
   "resolves contradictions", Hindsight "updates beliefs" — but no
   vendor publishes a stale-fact rate. We measure ours.
2. **Abstention** — LongMemEval's fifth dimension. unsupported_answer
   0.000 measured on both lanes. The field mostly returns top-k noise.
3. **Determinism + offline** — hash embeddings are byte-reproducible,
   no downloaded model, no LLM in the default derive path. Every other
   serious contender embeds with LLM/transformers (nondeterministic
   across runs, network/VRAM dependent).
4. **Scope isolation with measured leakage** — 5-D exact-equality,
   0.000 cross-scope leakage on both lanes, adversarial cross-scope
   decoy included.
5. **Immutable audit + rollback** — corrections are revisions with
   history; rollback restores prior heads. Unique in this set.
6. **Benchmark transparency** — open manifest, open runner, evidence
   files committed. The field's numbers are vendor-published and move
   with the harness; ours are reproducible.

## 4. Honest gaps (BEHIND / UNVERIFIABLE — not hidden)

| Gap | Detail | Plan |
|---|---|---|
| Conversation recall on hard corpora | LOCOMO recall 0.923 measured; Mem0 vendor 92.5, Hindsight/Supermemory lead LongMemEval — different harnesses, UNVERIFIABLE parity | Granite opt-in lifts paraphrase recall to 1.000; A-3 decision on default embeddings pending |
| Semantic embeddings default | Hash is default for determinism; Granite opt-in | A-3 adversarial evidence shows recall gain; decision tracked |
| Hosted API / managed service | Local-first only | G-2 publish verified, roadmap |
| Batch ingest at extreme scale | Chroma lane ~4 docs/s, we do 248/s at 1k; at 100k cold derive is super-linear (6,458s SQLite) — worker model mitigates | Scale workstream |
| Dreaming O(n²) at 10k+ | Consolidate 437s + curate 235s at 10k (measured) | LSH pre-filter candidate generation (P1d-opt), evidence-first |
| LongMemEval reproduction | We have NOT run LongMemEval on Severian | Would be the honest next benchmark — the 5 dimensions map directly to our measured capabilities |

## 5. Bottom line

Severian is not competing on "who scores higher on whose benchmark". It
competes on **measured correctness guarantees the field doesn't offer**:
0.000 stale, 0.000 leakage, 0.000 unsupported-answer, 1.000 temporal,
byte-determinism, immutable audit with rollback — all local-first, all
reproducible from committed evidence. The gaps (semantic embeddings by
default, hosted API, LongMemEval reproduction, 10k+ dream scale) are
documented with plans, not hidden.

---

*Policy: vendor numbers stay labelled; Severian numbers stay measured;
never mixed in one table. Update rules in `competitor-claims.md` §4.*
