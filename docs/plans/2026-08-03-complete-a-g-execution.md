# SEVERIAN — COMPLETE A–G EXECUTION PLAN (2026-08-03)

**Status:** IN PROGRESS — all phases will be marked `[READY TO BE AUDITED]` when done + tested.
**Owner:** KENSEI (executing) → then re-pass to 3rd-party agent for objective review.
**Decisions locked by Sahil (2026-08-03):**
- G-1 = **MERGE** audit branch into `main` (Severian).
- E-4 = **DELETE** the stale smoke file.
- Roadmap → kanban: HOLD (unchanged). No kanban conversion until Sahil says go.
- Honest-numbers taxonomy, TDD, mypy --strict, 5-D boundary, standalone product: unchanged.

---

## PLAN GOAL

Take Severian from the audited v0.1.0 state (all A–G issues registered, verified base
228/36 green) to a **complete, tested, auditable state**: every remaining issue in the
handover register §3 closed (implemented + tested), the roadmap's highest-leverage
techniques landed, the honesty/evidence chain intact, and the repo in a clean
state a third-party auditor can re-verify.

Completion = all phases DONE + tested + `[READY TO BE AUDITED]` markers set + master
handoff updated to reflect closure + re-pass to 3rd party.

---

## Phase map (A–G)

| Phase | Scope | Status |
|---|---|---|
| **Phase 0** | Verify base (git/pytest/mypy/ruff/PG/manifest/5-doc/1k scale) | DONE (base verified: 228/36, clean) |
| **Phase E** | P2 residuals: E-1 ordering, E-2 root policy, E-3 domain constraints, E-4 delete smoke, E-5 evidence re-stamp | IN PROGRESS |
| **Phase G** | Governance: G-1 merge audit→main, G-2 publish decision, G-3 CI doc, G-4 hold | PENDING |
| **Phase F** | Eval-process: all repo-side fixes already FIXED (F-1..F-4); F-5/F-6 auditor notes | VERIFIED DONE (see §F) |
| **Phase C** | Vendor-claims table (`competitor-claims.md`) | PENDING |
| **Phase B** | Conversation recollection: B-1 LOCOMO-tier benchmark, B-2 episodic summarization | PENDING |
| **Phase A** | Engineering gaps: A-1 Granite R2 embeddings, A-2 versioning/rollback, A-3 real-data probes, A-4 Chroma batch comparison | PENDING |
| **Phase D** | Weakness analysis implementation = A-1/A-2/A-4/B-2 (tracked under A/B) | PENDING |
| **Phase AUDIT** | Final state verification + update HANDOFF-MASTER + marker READY TO BE AUDITED | PENDING |

---

## PHASE 0 — VERIFY BASE (DONE)

Ran: git clean + branch + log; `uv run pytest -q` → **228 passed, 36 skipped, 3 warnings**;
mypy --strict / ruff not yet re-run this session (will run as part of each phase gate).
PG reachability + manifest validate + 5-doc repro + 1k scale: per §6 protocol (the
base was verified by the 3rd-party eval at e6ead47; re-verify at final AUDIT phase).

**Marker: DONE.**

---

## PHASE E — P2 RESIDUALS (IN PROGRESS)

### E-1 — `list_memories` ordering by UUID, not time
**Seam:** `src/severian/infrastructure/sqlite.py:554`, `postgresql.py:702` —
`.order_by(records.c.id)` → should order by `created_at` (stable tiebreak id).
**Risk:** low. Both backends have `created_at` columns.
**Tests:** unit/integration test asserting list_memories returns ascending created_at.
**Gate:** pytest + mypy + ruff.

### E-2 — approved-root policy for export/import destinations
**Seam:** `src/severian/cli/app.py:269-275` — only symlink refusal; missing root policy.
**Fix:** SEC-04 style: configurable approved roots (env/file), reject paths outside
roots + traversal + overwrite policy; keep symlink refusal.
**Tests:** CLI tests for allowed/denied roots, traversal, overwrite.
**Gate:** pytest + mypy + ruff.

### E-3 — domain models permit empty fields / unverified hashes
**Seam:** `src/severian/domain/models.py` — `Observation.content: str` (no min-length),
`content_hash: str = ""` (unverified), `Memory.content: str` (no min-length).
**Fix:** add constraints (min_length≥1 on content; hash validator: SHA-256 hex format
or empty-if-unset with explicit flag; document when empty is legal).
**Risk:** low; verify no test relies on empty content being legal.
**Gate:** pytest + mypy + ruff.

### E-4 — delete stale smoke file (DECIDED: DELETE)
**File:** `docs/benchmarks/phase3-smoke.json` — superseded by manifest-v1.json; its q5
rewards an unsupported answer. Delete it. Update any docs referencing it as a live
instrument (provider-benchmark.md, handover-brutal-critique.md keep as provenance).
`tests/benchmarks/test_manifest.py:32` uses the string "phase3-smoke" as a name — not a
file ref; harmless, leave.
**Gate:** grep confirms no code/test loads the file; pytest green.

### E-5 — evidence re-stamp (provenance)
**Seam:** `docs/benchmarks/evidence/2026-08-03/*.json` record SHA `241e68b`, but files
don't exist at that commit. Algorithm unchanged; recorded SHA not exact.
**Decision:** re-stamp evidence at current commit (re-run scale lanes 1k/10k — 100k is
too expensive; re-recorded provenance noting the 100k numbers are re-stamped copies
with caveat, or keep as-is with documented caveat). Pending decision: cheap re-run of
1k/10k + note.
**Gate:** evidence files reference the commit that produced them.

---

## PHASE G — GOVERNANCE (PENDING)

### G-1 — MERGE audit branch → main (DECIDED: MERGE)
- Ensure working tree clean, all phases E done first.
- `git checkout main && git merge audit/brutal-critique-v0.1.0 --no-ff` (or ff).
- Verify `git log --oneline main -1` shows merged HEAD; run full gate on main.
- Tag/note in commit message.
**Gate:** main == audit branch content; pytest/mypy/ruff on main.

### G-2 — publish decision (v0.1.0 wheel)
- Wheel + sdist exist in `dist/`. Decide: publish to a private index / PyPI / keep local.
- Default (no credential): document decision + install test (`uv pip install dist/*.whl`
  into a fresh venv, run `severian --help`). No credentials.
**Gate:** install test passes.

### G-3 — CI benchmark gate doc
- `.github/workflows/ci.yml` runs platform lanes only (chroma optional). Documented
  decision; add explicit comment/note in CI file or docs if not already.
**Gate:** doc present.

### G-4 — kanban HOLD (unchanged)
- No kanban conversion. Roadmap stays a document.

---

## PHASE F — EVAL-PROCESS (VERIFIED DONE)

- F-1..F-4 repo-side fixes: FIXED (gate count terminology, SDK anomaly, --chroma-venv,
  --manifest) — verified live in handover prompt + eval.
- F-5/F-6 auditor-side notes: document for next auditor; no repo work.

**Marker: DONE (verified).**

---

## PHASE C — VENDOR-CLAIMS TABLE (PENDING)

**Deliverable:** `docs/benchmarks/competitor-claims.md`
**Content:** each vendor claim (Mem0 LOCOMO/ECAI 2025, Hindsight, Honcho, SuperMemory,
Mempalace, Mnemosyne) → our measured number on the same dimension → verdict
(ahead/behind/unverifiable). Rule: same manifest, same harness, vendor-reported stays
labelled.
**Inputs:** results.md §5, manifest-v1.json, evidence, vendor pages (cited + dated).
**Gate:** table complete; every claim traced to evidence or vendor source + URL + date.

---

## PHASE B — CONVERSATION RECOLLECTION (PENDING)

### B-1 — LOCOMO-tier conversation-recall benchmark (roadmap benchmark #1)
- Build a LOCOMO-style typed-probe corpus (fact, multi-hop, temporal, adversarial,
  negative) on the existing manifest/runner protocol (query kinds already encoded).
- Run through our own harness — produces an honest conversation-recall number.
- Deliverable: new manifest + evidence file + results.md addition.
**Gate:** evidence file written; numbers recorded; honesty taxonomy respected.

### B-2 — Episodic summarization path (roadmap technique #4) — DESIGNED, PENDING
**Design (grounded in seams, ready to implement):**
- New `Memory(kind="episode")` produced by a consolidation pass per scope/session.
- `EpisodeService`: consolidates a session's observations into one episode whose
  `source_observation_ids` spans all session observations; content = budgeted
  summary (deterministic aggregation default — local-first, no LLM; optional
  LLM-judge merge/delete with rule-based fallback per memrefine/evodata).
- Job kind: add `episode` alongside `derive` in `DerivationWorker._derive` gate
  (worker.py:124) + enqueue path in ingestion; same lease/reap/idempotent drain.
- Retrieval: episodes indexed like any memory (FTS+vector); session recall query
  retrieves episode + raw observations.
- Tests: episode consolidation (deterministic), retrieval returns episodes,
  storage bounded, worker crash-safety (lease/reap).
**Gate:** pytest + mypy + ruff; re-run LOCOMO-tier manifest to show episode recall.

---

## PHASE A — ENGINEERING GAPS (PENDING)

### A-1 — Pluggable semantic embeddings (Granite R2 97M) — roadmap technique #1
- Add an embedding provider interface; default stays `HashEmbedding` (deterministic).
- Implement Granite R2 97M provider (ONNX/CPU; Apache-2.0; 384-dim) with hash fallback.
- Seam: `infrastructure/vector.py` `HashEmbedding` → provider protocol; composition
  wires provider; PG vector column dimension must be configurable (256→384).
- Embedding choice shifts accuracy +6.2pp (memdelta, 2606.29914) — worth it.
**Gate:** unit tests (provider encodes; fallback on missing model), mypy, ruff;
benchmark rerun shows no regression on manifest corpus.

### A-2 — Versioning / rollback (ChronoMem layer) — roadmap technique #3
- Supersession already exists; add version inspection + semantic rollback so a cold
  rebuild is recoverable (fixes W1/W2 risk surface).
- Seam: observation/memory revision chain + history/as-of APIs (already exist per
  HANDOFF §2 P0-5).
**Gate:** version list/rollback tests; mypy/ruff.

### A-3 — Real-data probes (future lane, documented)
- Keep documented; not run (synthetic gate is the release gate). Add to roadmap
  future-lane list.

### A-4 — Chroma batch-ingest comparison (roadmap benchmark #5)
- Add batch method to adapter protocol (or a batch ingest script) and compare
  Severian batch vs Chroma batch vs current serial.
- Seam: adapters; scale lane script.
**Gate:** evidence file; numbers recorded.

---

## PHASE D — WEAKNESS ANALYSIS IMPLEMENTATION

- D maps to A-1 (embeddings), A-2 (versioning/rollback), A-4 (batch), B-2 (episodic).
- Tracked under their phases; no separate work item.

---

## PHASE AUDIT — FINAL STATE + RE-PASS

1. Re-run full §6 protocol (git, pytest 228/36, mypy, ruff, PG, manifest, 5-doc, 1k scale).
2. Update HANDOFF-MASTER §3: mark every A–G issue CLOSED with evidence/commit.
3. Set `[READY TO BE AUDITED]` marker in this plan + handoff.
4. Deliver to Sahil → re-pass to 3rd-party agent for objective review.

---

## TRACKING / PROGRESS

| Item | Status | Evidence |
|---|---|---|
| Phase 0 base verify | DONE | 228/36 green |
| E-1 ordering | DONE | sqlite.py + postgresql.py order by created_at; test_list_memories_orders_by_created_at_not_uuid; list_audit also time-ordered |
| E-2 root policy | DONE | src/severian/cli/path_policy.py (approved roots, symlink-every-component, traversal); wired export/import; tests/unit/test_path_policy.py (10 tests) |
| E-3 domain constraints | DONE | models.py: content min_length=1 (Observation+Memory), SHA-256 hash validator; test_models.py |
| E-4 delete smoke | DONE | git rm docs/benchmarks/phase3-smoke.json; no code/test loads it |
| E-5 evidence re-stamp | IN PROGRESS | re-running 1k/10k at current commit (proc_cce60f77239d); 100k re-stamp + provenance note |
| G-1 merge → main | PENDING | — |
| G-2 publish decision | PENDING | — |
| G-3 CI doc | PENDING | — |
| F-1..F-4 | DONE | eval verified |
| C-1 competitor table | DONE | docs/benchmarks/competitor-claims.md (3 sections, verdicts) |
| B-1 LOCOMO-tier | DONE (honest, improving) | manifest-locomo-tier-v1.json + evidence/2026-08-03-locomo-tier/ + locomo-tier-analysis: after harness fixes (temporal via observation layer, stale-gate exclusion) recall 0.923, temporal 1.000, stale 0.000; residual = A-1 semantic gap + q9/q11 corpus artifacts; committed 2173c63, 783dcd3 |
| B-2 episodic | DESIGNED, PENDING | spec in plan §B-2 (episode Memory kind, EpisodeService, worker job kind, tests) |
| A-1 embeddings | PENDING | B-1 proves the gap; needs onnxruntime + model + PG dim migration — next major phase |
| A-2 versioning | PENDING | — |
| A-3 real-data probes | PENDING (doc) | — |
| A-4 batch compare | DONE | ingest_batch protocol + chroma override; measured chroma batch 22.8 vs per-doc 2.1 docs/s, severian 184.4 (8x faster than chroma best); evidence/2026-08-03-batch-compare/; committed 9566937 |
| D (impl of A/B) | PENDING | tracked w/ A/B |
| AUDIT final + marker | PENDING | — |

---

*Progress markers updated as each phase completes. Final marker: `[READY TO BE AUDITED]`.*
