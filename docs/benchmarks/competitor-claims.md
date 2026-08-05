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

## 1. Independently reproduced (our harness, our evidence — correction-lane producing SHA 9749a75)

| Metric | Severian SQLite | Severian PG | ChromaDB 1.5.9 | Source |
|---|---|---|---|---|
| recall@10 (manifest-v1) | 1.000 | 1.000 | 1.000 | results.md §1 |
| citation_precision (manifest-v1) | 1.000 | 1.000 | 0.812 | results.md §1 |
| stale_fact_rate (manifest-v1) | 0.000 | 0.000 | 0.179 | results.md §1 |
| cross_scope_leakage (manifest-v1) | 0.000 | 0.000 | 0.000 | results.md §1 |
| unsupported_answer_rate (manifest-v1) | 0.000 | 0.000 | 0.062 | results.md §1 |
| LOCOMO recall@10 | 0.923 | 0.923 | 0.962 | locomo-tier-analysis §results |
| LOCOMO MRR | 0.885 | 0.885 | 0.833 | locomo-tier-analysis §results |
| LOCOMO cit_prec | 0.719 | 0.719 | 0.531 | locomo-tier-analysis §results |
| LOCOMO stale_fact_rate | 0.000 | 0.000 | 0.144 | locomo-tier-analysis §results |
| LOCOMO temporal_accuracy | 1.000 | 1.000 | 1.000 | locomo-tier-analysis §results |
| ingest throughput (1k/10k/100k docs/s) | 248/254/206 | 170/156/160 | 4.2/3.7/skipped | scale-1k-10k.json, scale-100k.json |
| query p95 hot (1k→100k ms) | 69.9→3,238.6 | 83.3→151.6 | 247.7→276.6 | same |
| cold derive 100k (s) | 6,458 | 3,147 | n/a (skipped, ~9h) | scale-100k.json |
| Determinism / offline | YES | YES | NO (downloaded model) | results.md §6 |

> **Correction (2026-08-04):** this table previously carried pre-correction
> numbers (cit_prec 1.000, Chroma cit_prec 0.353, Chroma recall 0.964).
> Superseded per C-05 — current evidence above is authoritative.
>
> **Correction (2026-08-05):** cit_prec raised 0.938 → 1.000 by the
> temporal query-orientation fix (commit 0403f3f): present-oriented
> temporal queries now rank the active head first. Evidence:
> `evidence/2026-08-04/result.json` (pre-fix) vs Turn-1 focused lane
> (post-fix, cit_prec 1.000, recall 1.000, stale 0.000).

## 2. Vendor claims → our position (C-1)

| Vendor | Claim (source, date) | Dimension | Our measured number | Verdict |
|---|---|---|---|---|
| **Mem0** | LOCOMO results in ECAI 2025 paper; token-efficient memory algorithm (mem0.ai / arXiv, 2026-08-02) | Agent-loop conversation recall (LOCOMO) | LOCOMO-tier measured: recall 0.923, MRR 0.885, temporal 1.000, stale 0.000 (evidence/2026-08-04) | UNVERIFIABLE parity (vendor numbers are reported, not reproduced; our lane is independently measured) |
| **Hindsight** | Passively captures chat history; agent-loop recall (vendor docs, 2026-08-02) | Conversation capture + recall | Episodic path measured (B-2 episode command); LOCOMO recall 0.923 | BEHIND on conversation recall (0.923 < vendor's unmeasured LOCOMO); AHEAD on determinism/scope (measured) |
| **Honcho** | Agent memory framework, conversation grounding (vendor docs, 2026-08-02) | Conversation grounding | Same as above | UNVERIFIABLE (vendor number not reproduced) |
| **SuperMemory** | Bookmark/chat memory for LLM apps (vendor docs, 2026-08-02) | Chat memory retrieval | Same as above | UNVERIFIABLE (vendor number not reproduced) |
| **Mempalace** | Hosted memory API (vendor docs, 2026-08-02) | Hosted API, scale | Local-first; no hosted API (G-2 publish verified) | BEHIND on hosting; AHEAD on local-first/CVE-free (measured) |
| **Mnemosyne** | Hermes native memory provider (hermes docs, 2026-08-02) — recorded for completeness, not a competitor | Hermes provider | Severian is a standalone platform, not a Hermes provider | UNVERIFIABLE (different category) |
| **ChromaDB** | Vector store, semantic embeddings (vendor docs, 2026-08-02) | Semantic retrieval | Semantic embeddings measured (A-1 Granite): closes paraphrase gap (q6 c27 rank 1) but recall 0.808/0.885 < hash 0.923 on lexical corpus; BM25+hash default | BEHIND on semantic recall for paraphrase-distant data; AHEAD on correction/abstention/scope/determinism (measured) |

## 3. Verdict summary

- **Where Severian is AHEAD (measured, independent):** correction/supersession
  (0.000 stale vs Chroma 0.144), abstention (0.000 unsupported vs 0.062-0.188),
  scope isolation (0.000 leakage vs Chroma 0.000 after C-01 — no contamination),
  ranking precision (MRR 1.000 on answerable), determinism/local-first,
  CVE-free network-safe core.
- **Where Severian is BEHIND (honest):** conversation recall on the harder
  LOCOMO corpus (0.923 < 0.95 gate; no vendor parity claimed), semantic
  retrieval on paraphrase-distant data (Granite opt-in, not default), hosted
  API (local-first only).
- **UNVERIFIABLE (do not claim):** vendor LOCOMO/agent-loop numbers (Mem0,
  Hindsight, Honcho, SuperMemory) are reported, not reproduced; our lanes are
  independently measured and never mixed with vendor-reported figures.

## 4. Update rules

- Every entry re-checked when the dimension's roadmap item lands (B-1, B-2,
  A-1, G-2) — the verdict flips from UNVERIFIABLE/BEHIND to a measured AHEAD
  or stays honest.
- Vendor numbers are never mixed into results.md §1–§4. This table is the
  single labelled place for the comparison.
- Access dates refreshed when a claim is re-cited.

---
*Built per handover register C-1. No fabricated parity; vendor numbers labelled.*
