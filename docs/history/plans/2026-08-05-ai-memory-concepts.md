# AI Memory System — Concept Map and Gap Analysis (2026-08-05)

**Scope:** identify the full set of concepts a production AI memory system
needs, map each against what Severian has today, and propose the build order.
**Branch:** `feat/ai-memory-concepts`
**Baseline:** correction lane `fix/auditor-corrections-2026-08-04` (HEAD 0276815)

---

## 1. What Severian has today (ground truth from code + evidence)

| Layer | Capability | Where |
|---|---|---|
| Canonical store | Append-only observations (immutable raw evidence, P0-1), memories, entities, relations, jobs | `sqlite.py`, `postgresql.py` |
| Scope | 5-D boundary (tenant/profile/collection/agent/session), mandatory, exact-equality, fail-closed | `models.py`, `_scope_predicates` |
| Ingestion | Normalisation, idempotent derive jobs | `ingestion.py` |
| Derivation | Observation → memory extraction, local deterministic gateway (default), cloud-gated | `derivation.py` |
| Correction | Supersession (P0-5), revision chains, rollback (A-2), 0.000 stale (measured) | `service.py`, `memory_engine` tests |
| Projections | FTS5 + vector (hash default, Granite opt-in), rebuildable from canonical | `projections.py`, `fts.py`, `vector.py` |
| Retrieval | Hybrid RRF, abstention floor, scope-filtered, source-cited context | `retrieval.py` |
| Episodes | Session consolidation into Memory(kind=episode) | `episodes.py` |
| Worker | Async derive/episode jobs, leases, crash-safe drain | `worker.py` |
| Temporal | Observation-layer history fallback, temporal_accuracy 1.000 (measured) | adapter temporal path |
| CLI/API/MCP | ingest, search, rollback, episode; HTTP; MCP | `cli/`, `api/`, `mcp/` |

## 2. The full AI-memory concept map

Production AI memory systems (Mem0, Letta, Zep/Graphiti, Cognee, and the
cognitive-science literature) converge on these concepts. Each is marked
**HAVE / PARTIAL / MISSING** for Severian.

### Acquisition & representation
| Concept | Severian | Notes |
|---|---|---|
| Observation ingestion | HAVE | canonical, hashed, sourced |
| Fact/entity/relation extraction | HAVE (2026-08-05) | derive populates Entity/Relation via deterministic EntityExtractionService (P1c) |
| Session episode formation | HAVE (B-2) | |
| Multi-modal input | HAVE (2026-08-05) | Attachment metadata (image/audio/video/file/transcript) on observations; reference+digest only |
| Source provenance + confidence | HAVE | model_id, prompt_version, confidence |

### Maintenance & self-regulation (the "missing" cluster)
| Concept | Severian | Definition |
|---|---|---|
| **Self-curation** | **HAVE (2026-08-05)** | Periodic review: salience scoring, low-value candidates, contradiction flags, review markers — never deletes |
| **Dreaming / consolidation** | **HAVE (2026-08-05)** | Offline replay: deterministic Jaccard duplicate merge + contradiction flag + consolidated semantic memory. The hippocampal-replay analogue. |
| Memory decay / forgetting curve | HAVE (2026-08-05) | Salience-weighted retrieval (opt-in decay via salience_weight + halflife); curation recency half-life. Non-destructive. |
| Salience / importance weighting | HAVE (2026-08-05) | CurationService scores; RetrievalService applies opt-in decay-adjusted ranking; usage_count tracked. |
| Contradiction detection | HAVE (2026-08-05) | Dreaming pass flags high-overlap/different-content pairs; report-only, no auto-resolution. |
| Deduplication | HAVE (2026-08-05) | Derive dedupes by identity; dreaming merges near-duplicates (Jaccard >= 0.8) periodically. |
| Memory budgeting / limits | PARTIAL | ContextAssembler caps chars; no memory-count or token budget per scope. |
| Retention policy | HAVE (2026-08-05) | max_age_days/min_usage/kinds policy; evaluate (report) + apply_archive (opt-in, non-destructive). |

### Retrieval & use
| Concept | Severian | Notes |
|---|---|---|
| Hybrid retrieval (lexical+vector) | HAVE | RRF |
| Semantic embeddings | PARTIAL | Granite opt-in; not default |
| Abstention | HAVE | 0.000 unsupported (measured) |
| Temporal queries | HAVE | 1.000 (measured) |
| Multi-hop / graph retrieval | HAVE (2026-08-05) | GraphRetrievalService: BFS neighborhood + related_memories over Entity/Relation |
| Episodic vs semantic separation | PARTIAL | episode kind exists; no semantic layer above it |
| Rehearsal / retrieval-strengthening | MISSING | No usage-frequency feedback into ranking |

### Governance & ops
| Concept | Severian | Notes |
|---|---|---|
| Scope isolation | HAVE | 5-D, 0.000 leakage |
| Shared memory (partial scope) | HAVE (2026-08-05) | search_shared(agent_ids): opt-in multi-agent read, per-scope isolation preserved |
| Audit trail | HAVE | immutable canonical + audit records |
| Determinism | HAVE | byte-identical runs |
| Offline / local-first | HAVE | |
| Hosted API | MISSING | |

## 3. Prioritised build order

**Tier 1 (THIS BRANCH — built and tested):**
1. **Dreaming (consolidation)** — DONE. `ConsolidationService` in
   `src/severian/application/dreaming.py`: deterministic Jaccard duplicate
   detection (>= 0.8 overlap), non-destructive merge (older superseded via
   new `supersede_memory` port method, survivor keeps provenance, audit
   recorded, `consolidated` memory emitted), contradiction flags (0.6-0.8
   overlap band, report-only). Worker `dream` job kind (mode=consolidate).
   **P1d scale fix (2026-08-05):** MinHash-LSH pre-filter (24 bands x 2
   rows, stopword-filtered signatures) accelerates BOTH the contradiction
   scan AND the duplicate-merge grouping: both use the same LSH candidate
   generator (`detect_contradictions` / `_lsh_candidates`) with exact
   Jaccard as the final decision. Measured 10k consolidate 437s → 131s on
   a diverse corpus. What remains bounded/worst-case (NOT LSH-accelerated):
   dense corpora where every document shares a structural template (LSH
   candidate ratio → 1.0, no pruning possible), and the per-group merge
   writes (supersede + consolidated memory emission), which are inherent
   to the workload. LSH is a candidate generator only; the merge/contradiction
   decisions stay exact Jaccard.
2. **Self-curation** — DONE. `CurationService`: salience scoring
   (0.4*confidence + 0.3*recency + 0.3*usage, 720h half-life), low-value
   CANDIDATES (never deleted), contradiction pairs, `curation_review`
   marker memory. Worker `dream` job kind (mode=curate).
   Tests: `tests/application/test_dreaming.py` (7 tests: jaccard, merge
   non-destructive, contradiction no-mutation, scope-bound, curation
   proposes-not-deletes, worker consolidate, worker curate).

**Tier 2 (THIS BRANCH — built and tested):**
1. **Usage-count tracking** — DONE. `Memory.usage_count` field +
   `record_memory_usage` port (SQLite/PG/fake): increments on retrieval
   hits (opt-in `track_usage=True` on `MemoryService.search`), skips
   superseded heads.
2. **Salience-weighted ranking** — DONE. `RetrievalService.search`
   gains opt-in `salience_weight` + `salience_halflife_h`: boosts recent/
   frequently-used memories deterministically (0.5*recency + 0.5*usage,
   saturating at 100 hits). Default 0.0 = unchanged pure-relevance
   behaviour (benchmark-safe).
3. **Retention policy** — DONE. `RetentionPolicyService`: policy dict
   (max_age_days / min_usage / kinds); `evaluate` reports candidates
   non-destructively; `apply_archive` (opt-in) supersedes candidates with
   a `retention_archive` marker (canonical store keeps everything, P0-1).
4. **Contradiction scan service** — DONE. `ContradictionScanService`
   standalone (extracted pattern from dreaming): report-only, never
   mutates.
5. **Worker dream modes** — DONE. `dream` job kind now supports
   consolidate / curate / retention / retention-archive / contradictions.
   Tests: `tests/application/test_tier2_memory.py` (12 tests).

**Tier 3 (THIS BRANCH — built and tested):**
1. **Graph retrieval** — DONE. `GraphRetrievalService`
   (`src/severian/application/graph.py`): entity-centered BFS
   neighborhood traversal (both directions, temporal validity filtering —
   expired/not-yet-valid relations excluded), `related_memories` scoring
   by min graph distance. New port methods `list_entities`,
   `list_relations`, `get_relation` on SQLite + PostgreSQL + fake.
   Pattern: entity-linking + graph traversal (Mem0/Graphiti style).
2. **Multi-modal ingestion** — DONE. `Attachment` domain model
   (kind image|audio|video|file|transcript, uri, media_type, SHA-256
   digest validated, alt_text/transcript, size); `Observation.attachments`
   list; `IngestionService.ingest(attachments=...)`. Canonical store keeps
   reference + digest only (text-first, deterministic); derivation/
   retrieval operate on the text content.
3. **Shared-scope visibility** — DONE. `RetrievalService.search_shared`
   (opt-in): queries each agent's exact 5-D scope independently and
   merges deterministically (dedup by memory id, fused-score re-rank).
   Caller-declared visibility rule; no wildcard; isolation boundary
   preserved per scope.
   Tests: `tests/application/test_tier3_memory.py` (9 tests).

**Tier 3 status: COMPLETE.** All concept-map items are now HAVE/DONE.
Remaining future work: retrieval-time usage feedback into ranking
(rehearsal), memory budgeting per scope, hosted API.

## 4. Design constraints (locked by existing decisions)

- Determinism: all new passes must be byte-reproducible (no LLM in the
  default path; gateway optional like derive).
- Non-destructive: canonical store is append-only; dreaming/curation
  supersede + mark, never delete.
- Scope-bound: every pass operates per-scope, respects the 5-D boundary.
- No tech debt: tests + mypy --strict + ruff for every new module.
- Honest numbers: any new capability measured with the existing harness
  before claiming value.

---

*Written 2026-08-05. Gap analysis grounded in code inspection + measured
evidence; competitor features are vendor-reported (see competitor-claims.md).*

---

## Session status — 2026-08-05 (release programme COMPLETE, merged to main)

**HEAD:** `3eb2db5` (main, merge of `feat/ai-memory-concepts` 7968319) — pushed.
**Full suite:** 433 passed / 36 skipped / 1 warning (third-party httpx2). ruff clean.
mypy --strict: one documented pre-existing exclusion (`granite_embedding.py:58`,
optional sentence_transformers import).

### All 36 objective-review items completed
- **Security:** SEC-01 principal auth, SEC-02 admin audit trail, SEC-03 0600 perms
- **Data:** DATA-01..07 (import provenance, staged atomic restore, correction
  durability, atomic graph+memory publish, collection ownership, batch scope
  lookup, atomic usage count)
- **Ops:** JOB-01 reaper wiring; OPS-01 outbox projector, OPS-02 tombstone
  erasure, OPS-03 bounded sync derive, OPS-04 shared-memory ACL policy,
  OPS-05 idempotent consolidation/retention, OPS-06 failure-atomic preflight,
  OPS-07 dead-letter inspect/requeue (CLI: `severian dead-letter`)
- **Arch:** ARCH-01 composition roots collapsed (MCP via build_bundle),
  ARCH-02 single projection contract, ARCH-03 5-D boundary (domain text/hits
  modules, LexicalIndex protocol), ARCH-04 no Memory proxy, ARCH-05 CLI work
  uses canonical derivation (graph extractor)
- **Evidence:** EVID-01..10 — CI bootstrap, current-HEAD benchmark package
  (`docs/benchmarks/evidence/2026-08-05-head/`), adversarial-27 reconciled
  (recall 1.000 / MRR 1.000 / cit_prec 1.000 / stale 0.000 — gate PASS),
  LOCOMO reconciled (recall 0.962 / MRR 0.962 / cit_prec 0.844), dreaming
  provenance, schema migration tests, warnings 3→1, 433-test count verified.

### Remaining caveats (next session, if picked up)
1. **PostgreSQL benchmark lane** not re-run at HEAD — requires `--pg-dsn` /
   `SEVERIAN_PG_DSN`; last measured 2026-08-04 (9749a75, PASS). Disclosed in
   `docs/benchmarks/evidence/2026-08-05-head/README.md`.
2. **LOCOMO gate still FAILS** on citation_precision (0.844 < 1.0) and
   unsupported_answer_rate (0.062 > 0). Tracked follow-up; improved from
   2026-08-04 (0.719 / 0.188) but not closed.
3. Release posture: CONDITIONAL (trusted single-user, loopback); BLOCKED for
   networked/multi-user until the above two close.
4. Historical branches kept: `audit/brutal-critique-v0.1.0`,
   `fix/auditor-corrections-2026-08-04`. Worktree residue cleaned.

*Session context appended 2026-08-05. Full gate record in
`docs/review/objective-review-2026-08-05.md`.*
