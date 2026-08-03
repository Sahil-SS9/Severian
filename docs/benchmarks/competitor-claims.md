# Competitor Claims — Severian vs Vendor-Reported Numbers

**Date:** 2026-08-03
**Policy:** Same manifest, same harness, vendor-reported stays labelled.
Every vendor claim below is from the provider's own materials (URL + access date),
never mixed into independently reproduced tables (results.md §1–§4).
**Verdict key:** AHEAD (Severian's measured number beats the vendor's claimed
dimension on our harness) · BEHIND (vendor leads on a dimension we cannot yet
match) · UNVERIFIABLE (vendor claims a dimension we have not measured or that
uses a different corpus/conditions — no false equivalence).

---

## 1. Independently reproduced (our harness, our evidence)

| Metric | Severian SQLite | Severian PG | ChromaDB 1.5.9 | Source |
|---|---|---|---|---|
| recall@10 | 1.000 | 1.000 | 0.964 | results.md §1 |
| citation_precision | 1.000 | 1.000 | 0.353 | results.md §1 |
| stale_fact_rate | 0.000 | 0.000 | 0.179 | results.md §1 |
| cross_scope_leakage | 0.000 | 0.000 | 0.000 | results.md §1 |
| unsupported_answer_rate | 0.000 | 0.000 | 0.062 | results.md §1 |
| ingest throughput (1k/10k/100k docs/s) | 248/254/206 | 170/156/160 | 4.2/3.7/skipped | scale-1k-10k.json, scale-100k.json |
| query p95 hot (1k→100k ms) | 69.9→3,238.6 | 83.3→151.6 | 247.7→276.6 | same |
| cold derive 100k (s) | 6,458 | 3,147 | n/a (skipped, ~9h) | scale-100k.json |
| Determinism / offline | YES | YES | NO (downloaded model) | results.md §6 |

## 2. Vendor claims → our position (C-1)

| Vendor | Claim (source, date) | Dimension | Our measured number | Verdict |
|---|---|---|---|---|
| **Mem0** | LOCOMO results in ECAI 2025 paper; token-efficient memory algorithm (mem0.ai / arXiv, 2026-08-02) | Agent-loop conversation recall (LOCOMO) | **Not yet measured** — B-1 LOCOMO-tier not run | UNVERIFIABLE (dimension unmeasured; must run B-1 before any claim) |
| **Hindsight** | Passively captures chat history; agent-loop recall (vendor docs, 2026-08-02) | Conversation capture + recall | No episodic path (B-2 pending); retrieval is deterministic, scope-bound | BEHIND on conversation recall until B-1/B-2; AHEAD on determinism/scope (measured) |
| **Honcho** | Agent memory framework, conversation grounding (vendor docs, 2026-08-02) | Conversation grounding | Same as above | UNVERIFIABLE (dimension unmeasured) |
| **SuperMemory** | Bookmark/chat memory for LLM apps (vendor docs, 2026-08-02) | Chat memory retrieval | Same as above | UNVERIFIABLE (dimension unmeasured) |
| **Mempalace** | Hosted memory API (vendor docs, 2026-08-02) | Hosted API, scale | Local-first; no hosted API (G-2 publish pending) | BEHIND on hosting; AHEAD on local-first/CVE-free (measured) |
| **Mnemosyne** | Hermes native memory provider (hermes docs, 2026-08-02) — recorded for completeness, not a competitor | Hermes provider | Severian is a standalone platform, not a Hermes provider | UNVERIFIABLE (different category) |
| **ChromaDB** | Vector store, semantic embeddings (vendor docs, 2026-08-02) | Semantic retrieval | No semantic embeddings (A-1 pending); BM25+hash deterministic | BEHIND on semantic recall until A-1; AHEAD on correction/abstention/scope/determinism (measured) |

## 3. Verdict summary

- **Where Severian is AHEAD (measured, independent):** correction/supersession
  (0.000 stale vs 0.179), abstention (0.000 unsupported vs 0.062), scope
  isolation (0.000), ranking precision (1.000), determinism/local-first,
  CVE-free network-safe core.
- **Where Severian is BEHIND (honest):** conversation recall (no B-1 number,
  no B-2 episodic path), semantic retrieval (no embeddings until A-1), hosted
  API (nothing published until G-2).
- **UNVERIFIABLE (do not claim):** any LOCOMO/agent-loop conversation number
  until B-1 runs on our own harness; any semantic-retrieval parity until A-1.

## 4. Update rules

- Every entry re-checked when the dimension's roadmap item lands (B-1, B-2,
  A-1, G-2) — the verdict flips from UNVERIFIABLE/BEHIND to a measured AHEAD
  or stays honest.
- Vendor numbers are never mixed into results.md §1–§4. This table is the
  single labelled place for the comparison.
- Access dates refreshed when a claim is re-cited.

---
*Built per handover register C-1. No fabricated parity; vendor numbers labelled.*
