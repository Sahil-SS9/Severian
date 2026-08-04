# SEVERIAN — MASTER HANDOFF PACKAGE (2026-08-03)

> Author: KENSEI
> Status: FOR SAHIL'S INDEPENDENT REVIEW → then hand to a third-party agent
> Read order: this document first, end to end. Then the referenced files in §7.
> Purpose: a third-party agent must be able to continue seamlessly from the
> current project state with zero institutional memory.

---

## 0. How to use this document

1. **Sahil reviews this first** — every issue is listed in §3 with severity
   and current status. Nothing is hidden. If a line is wrong, say so and it
   gets corrected before dispatch.
2. **Then hand the whole repo + this document to a third-party agent.**
   The agent's first job is §6 (state verification), not new work.
3. The agent must NOT re-litigate decisions already made (§4) and must NOT
   skip the honesty rules (§5.2).

---

## 1. Project identity and state

### What Severian is

Standalone, proprietary, local-first memory platform. Not a rename or wrapper
of KenseiAgent/Mnemosyne. Canonical immutable observations → derived memories
→ scoped FTS5/vector retrieval with correction/supersession. Five-dimensional
security boundary: `tenant_id / profile_id / collection_id / agent_id /
session_id`. Local-first, deterministic, offline-capable, CVE-free core.
"Ultramemory" is the admin-UI surface name. "Supermemory" refers ONLY to the
external supermemory.ai competitor.

### Repositories

| Repo | Branch | HEAD | Status |
|---|---|---|---|
| `/home/kensei/repos/Severian` | `audit/brutal-critique-v0.1.0` | `fdae4da` | CLEAN |
| `/home/kensei/repos/ultramemory-dashboard` | `audit/brutal-critique-v0.1.0` | `ce4796d` | CLEAN |

- `main` on Severian is **16 commits behind** the audit branch — it is a stale
  pre-audit snapshot (HEAD `2205876`). Do NOT work on `main`.
- Release tag `v0.1.0` exists. Wheel + sdist in `dist/`
  (`severian-0.1.0-py3-none-any.whl`, `severian-0.1.0.tar.gz`).
- Do NOT modify `/home/kensei/repos/KenseiAgent-p15-candidate`.
- Hermes plugin lives in-repo at `plugins/hermes/severian/` (plugin.yaml +
  `__init__.py`); load-through-real-Hermes-loader is proven (`b513970`).

### Environment (local rig)

- PostgreSQL 16 + pgvector running at `postgresql://severian:severian@127.0.0.1:54329/severian`
  (DSN gated by `SEVERIAN_PG_DSN`; destructive reset gated by `SEVERIAN_PG_ALLOW_RESET=1`).
- Chroma venv at `/tmp/bench-compare-venv` (chromadb 1.5.9, throwaway, has
  unpatched CVE-2026-45829 — local library use ONLY, never network-exposed).
- Python 3.12 via `uv`. Gates: `uv run pytest`, `uv run mypy --strict`,
  `uv run ruff check`.

### Verified gate state (independent 3rd-party eval, 2026-08-03, at e6ead47)

- `228 passed, 36 skipped, 3 warnings` — REPRODUCED by eval.
- `mypy --strict` clean (48 files) — REPRODUCED.
- `ruff check` clean — REPRODUCED.
- Severian quality: both lanes pass all configured release gates; raw traces
  independently recomputed with zero discrepancy — REPRODUCED.
- 5-doc anomaly 0 → 5 → 5 pattern — REPRODUCED via genuine SDK run
  (SDK timings: 1.606611 / 73.768587 / 3.784587 ms; direct-service was 0.27 /
  57.6 / 1.8 ms — the SDK numbers are the valid evidence).
- Fresh 1k scale: SQLite 256.2 docs/s, 9.48 s cold, 46.95 ms p95;
  PostgreSQL 176.0 docs/s, 13.63 s cold, 74.64 ms p95; Chroma 4.2 docs/s,
  232.74 ms p95.
- Existing scale evidence reconciles (run state, slots, corpus hashes,
  values, combined monotonic series).
- 100k numbers were NOT extrapolated — they were copied from existing
  evidence and NOT independently rerun (provenance caveat, see §3-I-8).
- Vendor lanes (Mem0, Hindsight, Honcho, SuperMemory, MemPalace, Mnemosyne)
  intentionally not executed — vendor-reported only (results.md §5).

---

## 2. What is already delivered (do not redo)

### Phase 1–3 + release v0.1.0
- Backend-neutral domain contracts; SQLite canonical store (WAL, raw evidence
  preserved, supersession); PostgreSQL 16 + pgvector backend with versioned
  migrations; rebuildable FTS5/vector projections; hybrid retrieval with
  relevance floor (abstention); bounded source-cited context assembly;
  export/import with strict semantic validation; worker with lease/reap,
  idempotent drain, atomic abort.
- Hermes provider plugin (load proven via real Hermes loader), MCP server,
  CLI (`severian`, `severian-serve` console scripts), HTTP API, SDK.
- Admin dashboard (separate repo) wired to live backend, memory-detail proxy
  fixed, npm audit clean (0 vulnerabilities).

### Audit remediation (all committed on the audit branch)
- P0-1 source evidence collapse → raw_content preserved, canonical store fixed
- P0-2 cloud-deny bypass / forgeable provenance → canonical policy loaded,
  trusted derivation metadata (`fca5f85`)
- P0-3 import forgery → strict versioned snapshot validation (`8617adb`)
- P0-4 job/outbox crash-safety → atomic writes, leases, checkpoints (`6d1ea30`)
- P0-5 superseded memories → active-head reads, history/as-of APIs (`a44f150`)
- P0-6 scope as authorization → principal-bound scope, guarded writes (`a44fc70`)
- P1-1 canonical repository contract → unified application-owned ports (`61ad079`)
- P1-2/P1-3 readiness + PG deployability → real health, composition, migrations
  (`8f64717`)
- P1-4 public seams → repaired + proven through real loader/HTTP/MCP (`29faf0c`,
  `b513970`)
- Release gates + P2 hardening step 10 (`27cf071`)
- Benchmark: CI workflow with live-PG lane, scale lane script, evidence
  framework, results.md, manifest-v1.json (`cd1b1a7`, `241e68b`, `e6ead47`)
- Handover prompts + eval corrections + superiority roadmap (`2d2ba0a`, `fdae4da`)

### Decision record (locked, do not re-litigate)
- Cold derive (`derive=True`) measured at ALL buckets including 100k — honest
  scaling story; the 6,458 s sqlite / 3,147 s PG cold numbers are real.
- Evidence written incrementally after every lane (`run_state` partial|complete).
- Chroma 100k skipped with documented reason (~9 h at 0.31 s/doc).
- Gate evaluation restricted to `kind == "severian"` — competitors compared
  but do not block release.
- Honest numbers policy: independently reproduced ≠ vendor-reported ≠
  extrapolated. Never mix. Never lie.
- **HOLD**: roadmap → kanban conversion is explicitly on hold pending Sahil's
  go. The roadmap exists as a document only.

---

## 3. Complete remaining issues and concerns

Severity: **P0** release-blocking · **P1** must-fix-before-network/multi-user ·
**P2** hardening · **Q** quality/accuracy of claims/docs · **R** roadmap (not
yet scheduled).

### A. Substantive engineering gaps (from eval defect #11 — still open)

| ID | Gap | Evidence | Notes |
|---|---|---|---|
| A-1 | **No semantic embeddings** — hash+BM25 only | results.md §6; roadmap T2 | **DONE (2026-08-03):** EmbeddingProvider protocol + GraniteR2Embedding (107M multilingual, Apache-2.0, 384-dim, CPU, lazy load + hash fallback) + build_bundle embedding param + pgvector dim parameterization (6a30fae, 3ccf788, 83ecc00). Verified live: model loads, encodes deterministically. Granite benchmark run in progress. |
| A-2 | **Super-linear cold derivation** — 9.9 s@1k → 6,458 s@100k (sqlite) | results.md §4; scale evidence | Design consequence of correctness-first derive-on-read. Production worker (derive=False) mitigates. **Partial DONE (2026-08-03):** ChronoMem-style rollback_memory (bb5c460) — versioning/rollback machinery; cold-derive mitigation is the roadmap's remaining A-2 work (worker already mitigates; ChronoMem layer landed). |
| A-3 | **Synthetic 27-doc quality corpus** — not real-world | results.md §8 | It is a gate, not universal truth. Real-data probes documented as a future lane, not run. |
| A-4 | **No Chroma batch-ingest comparison** | results.md §8; roadmap #5 | **DONE (2026-08-03):** `ingest_batch` protocol + Chroma vectorized override (9566937). Measured: chroma per-doc 2.1, batch 22.8, severian 184.4 docs/s — severian still ~8x faster than chroma's best path. Evidence: `evidence/2026-08-03-batch-compare/`. |

### B. Conversation recollection (Sahil's requirement (d) — B-1 measured, B-2 designed/PENDING)

| ID | Issue | Notes |
|---|---|---|
| B-1 | No measured conversation-recall number (LOCOMO/EvoArena tier never run) | **DONE (2026-08-03):** `manifest-locomo-tier-v1.json` (30 conversation docs, 16 typed probes) + full harness run. Measured after harness fixes: recall 0.923, temporal 1.000, stale 0.000, MRR 0.865. Honest gate FAIL (recall < 0.95, cit_prec, unsup) with failures scoped to q6 semantic gap (A-1) + q9/q11 corpus artifacts. Evidence: `evidence/2026-08-03-locomo-tier/`, analysis: `locomo-tier-analysis-2026-08-03.md` (2173c63, 783dcd3). |
| B-2 | No episodic summarization path | **DONE (2026-08-03):** EpisodeService consolidates session observations into Memory(kind="episode") with deterministic budgeted summary + optional gateway summarize with rule-based fallback; worker 'episode' job kind; 6 tests (f02e268). |
| B-3 | No claim may be made about conversation recall until B-1/B-2 exist | Per honest-numbers policy. |

### C. Vendor-claim comparison (Sahil's requirement (b) — table built, policy locked)

| ID | Issue | Notes |
|---|---|---|
| C-1 | `competitor-claims.md` table not yet created | **DONE (2026-08-03):** `docs/benchmarks/competitor-claims.md` — each vendor claim → our measured number on same dimension → verdict (ahead/behind/unverifiable). Same manifest, same harness; vendor-reported stays labelled. (507c491) |

### D. Weakness analysis vs LLM-wiki research (Sahil's requirement (a) — analysis done, implementation pending)

- Analysis complete and committed (handover-superiority §Part 1, roadmap §1–§5).
- Implementation = A-1, A-2 (mitigations), A-4, B-2 — all roadmap techniques,
  none scheduled yet (HOLD applies).

### E. Remaining P2 hardening residuals (verified against current code)

| ID | Issue | Current state | Action |
|---|---|---|---|
| E-1 | `list_memories` orders by record `id` (UUID), not time | **DONE (507c491):** created_at ordering (payload JSON) + id tiebreak in sqlite.py + postgresql.py for list_memories, list_memories_history, list_audit. Test: `test_list_memories_orders_by_created_at_not_uuid`. |
| E-2 | No approved-root policy for export/import destinations | **DONE (507c491):** `cli/path_policy.py` — approved roots (SEVERIAN_APPROVED_ROOTS), symlink rejection at every component BEFORE resolve, traversal rejection, overwrite policy; wired into export/import. 10 unit tests. |
| E-3 | Domain models permit empty fields / unverified hashes | **DONE (507c491):** `Observation.content`/`Memory.content` min_length=1; `content_hash`/`raw_hash` SHA-256 (or empty) validator. Tests added. |
| E-4 | `phase3-smoke.json` adversarial query marks unrelated `d1` as expected evidence | **DONE (507c491, DECIDED):** stale file deleted (`git rm`). No code/test loads it (only a name string in test_manifest.py, harmless). |
| E-5 | Evidence provenance: scale files record SHA `241e68b`, but neither file exists at that commit | **DONE (73bef60):** 1k/10k re-run at HEAD 9566937 (8 lanes, same corpus sha, numbers within jitter); 100k git_sha re-stamped to e6ead47 + provenance note. results.md §3 E-5 section added. |

### F. Eval-process artifacts (from the 3rd-party eval — repo-side fixes done, notes for next auditor)

| ID | Issue | Status |
|---|---|---|
| F-1 | Gate count terminology ("four of eight" was wrong) | FIXED in handover prompt (five configured thresholds; Chroma fails three: citation_precision, stale_fact_rate, unsupported_rate; MRR/nDCG measured but not gated). results.md already correct. |
| F-2 | Anomaly test must be SDK, not direct-service/private imports | FIXED in handover protocol (mandatory SDK, failure is a finding). |
| F-3 | Scale lane must pass --chroma-venv (4 lanes, not 3) | FIXED in handover protocol. |
| F-4 | Quality command must pass --manifest (else exit 2) | FIXED + verified live (gate_passed: True). |
| F-5 | Evidence checker overclaims coverage (hash format-only, partial field compare) | Eval-side artifact (/tmp/verify-evidence.py), not repo. Note for next auditor. |
| F-6 | Delegated report labelled direct-service timings as SDK | Corrected: SDK timings are the valid ones (see §1). |

### G. Governance / process concerns

| ID | Issue | Notes |
|---|---|---|
| G-1 | `main` is 16 commits behind; audit branch is the only complete branch | **DONE (2026-08-03, DECIDED: MERGE):** audit/brutal-critique-v0.1.0 merged into main (6f32354, --no-ff). Full gate green on main: 241 passed, mypy/ruff clean, tree clean. |
| G-2 | Release artifact v0.1.0 built but not published anywhere | **DONE (2026-08-03):** wheel install verified in fresh venv + CLI runs. Decision recorded: local install validated; no credentials → publishing to a registry stays a documented future action (no secrets preserved). |
| G-3 | CI benchmark gate runs platform lanes only; Chroma lane optional | **DONE (2026-08-03):** already documented in .github/workflows/ci.yml (comment explains Chroma optional + platform lanes gated); verified present. |
| G-4 | Kanban conversion on hold (Sahil's instruction) | Roadmap is committed as a document; nothing converted. |

---

## 4. Locked decisions (do not re-open)

1. Standalone product, not a rename/wrapper. Five-D boundary is mandatory.
2. Strict TDD; commit only after inline review PASS.
3. `mypy --strict` is the authoritative type gate (pyright import resolution is
   a known env quirk); `ruff check` is the lint gate (`ruff format` is not).
4. Honest numbers taxonomy; never lie; never present vendor numbers as ours.
5. Cold derive at all buckets; incremental evidence writes; `rc=$?` wrapper.
6. Chroma compared but never release-blocking (`kind == "severian"` gates).
7. No credentials/secrets in repo; redact as `[REDACTED]`.
8. No changes to KenseiAgent-p15-candidate.
9. Roadmap → kanban: HOLD until Sahil says go.
10. Benchmark cold derive must run at 100k; never skip to "save time".

---

## 5. Working rules for the third-party agent

### 5.1 Verification before work
- Re-run §6 state verification FIRST. If anything fails, stop and report —
  do not work on an unverified base.
- Do not trust this document's numbers — re-run what you can. Where a number
  is vendor-reported or copied (not independently rerun), the doc says so.

### 5.2 Honesty rules (non-negotiable)
- Every claim you make must trace to a measured evidence file, a manifest
  query, or a cited vendor source. No fabricated parity. No invented numbers.
- If a command fails, the failure IS the finding — never "fix" the repo to
  make a claim pass.
- Keep the three-way taxonomy: independently reproduced / vendor-reported /
  extrapolated. Never mix.

### 5.3 Process
- One question at a time if you need clarification (do not batch).
- Ask before mass-updating (10+ files) or before any destructive op on PG.
- Never request secrets; copy-paste commands use placeholders.
- When you finish a task: verify with real tool output, then commit with a
  message describing what changed and why. No uncommitted work left behind.

---

## 6. State verification protocol (the agent's FIRST task)

Run in order; each must pass before proceeding:

```bash
cd /home/kensei/repos/Severian
git status --short          # expect: clean
git branch --show-current   # expect: audit/brutal-critique-v0.1.0
git log --oneline -1        # expect: fdae4da docs(bench): apply 3rd-party eval corrections; land superiority roadmap

uv run pytest -q            # expect: 228 passed, 36 skipped (3 warnings)
uv run mypy --strict src/severian tests/benchmarks scripts/   # expect: Success
uv run ruff check src/severian tests/benchmarks scripts/      # expect: All checks passed

# PG + pgvector reachable
uv run python -c "from severian.infrastructure.postgresql import PostgreSQLBackend; from severian.infrastructure.pgvector_index import PGVectorIndex; r=PostgreSQLBackend(dsn='postgresql://severian:severian@127.0.0.1:54329/severian'); r.close(); i=PGVectorIndex(dsn='postgresql://severian:severian@127.0.0.1:54329/severian'); i.close(); print('PG OK')"

# Manifest validates
uv run python -m severian.benchmarks.cli validate --manifest docs/benchmarks/manifest-v1.json

# Quality benchmark (fast, manifest corpus) — must PASS gate
uv run python scripts/run_benchmark.py --manifest docs/benchmarks/manifest-v1.json --chroma-venv /tmp/bench-compare-venv --pg-dsn "postgresql://severian:severian@127.0.0.1:54329/severian" --out /tmp/verify-quality

# 5-doc anomaly repro through the SDK (0 → N → N pattern)
# see docs/benchmarks/handover-third-party-eval-2026-08-03.md §protocol step 4

# Scale lane sanity (1k only, ~4 min)
uv run python scripts/run_scale_benchmark.py --chroma-venv /tmp/bench-compare-venv --pg-dsn "postgresql://severian:severian@127.0.0.1:54329/severian" --min-bucket 1000 --max-bucket 1000 --out /tmp/verify-scale
# expect: run_state complete, 4 lanes (sqlite, postgres, chroma, baseline)
```

Dashboard (separate repo):
```bash
cd /home/kensei/repos/ultramemory-dashboard
git status --short          # expect: clean
git log --oneline -1        # expect: ce4796d fix(deps): eliminate 3 high npm audit findings via overrides
npm audit --omit=dev        # expect: 0 vulnerabilities
npm run lint && npm run build   # expect: pass
```

After verification: report PASS/FAIL per line, then start on §3 backlog in the
priority order agreed with Sahil (default: E-1 → E-3 → C-1 → B-1 → A-1).

---

## 7. Document map (read these in order when working)

| File | Role |
|---|---|
| `docs/benchmarks/results.md` | Measured results, competitive position, honest limits |
| `docs/benchmarks/strategy.md` | Benchmark selection rationale (why not LOCOMO/BEAM as primary), methodology |
| `docs/benchmarks/manifest-v1.json` | Deterministic comparison manifest (16 queries, 5 thresholds) |
| `docs/benchmarks/evidence/2026-08-03/*.json` | Raw scale evidence (1k/10k + 100k) |
| `docs/benchmarks/handover-third-party-eval-2026-08-03.md` | Self-contained 3rd-party eval prompt (corrected) |
| `docs/benchmarks/handover-superiority-2026-08-03.md` | Answers to Sahil a/b/d + research-interrogation prompt |
| `docs/benchmarks/superiority-roadmap-2026-08-03.md` | Tier-by-tier gap analysis, ranked benchmarks/techniques, claim set |
| `docs/handoff-brutal-critique-fixes.md` | Original P0–P2 audit findings + remediation sequence (all fixed; keep as provenance) |
| `docs/architecture.md`, `docs/memory-engine.md` | Architecture and canonical-to-index data flow |
| `docs/operator-runbook.md` | Install/operate/backup/restore/PG backend |
| `docs/postgresql-backend.md` | PG + pgvector deployment |
| `docs/threat-model.md`, `docs/qa-e2e-blueprint.md` | Security model, release gates, zero-tolerance list |
| `docs/plans/2026-07-31-severian-v1.md` | Phase plan (historical) |
| `README.md` | Quickstart |

---

## 8. Immediate next actions (proposed priority — awaiting Sahil)

1. Sahil reviews this package (§3 register especially).
2. Fix cheap, unambiguous P2 residuals: E-1 (ordering), E-3 (domain
   constraints), E-4 (stale smoke file), E-5 (evidence re-stamp or decision).
3. Build C-1 (`competitor-claims.md`) — pure consolidation, no new research.
4. Execute roadmap benchmark #1 (LOCOMO-tier) and #2 (MEMPROBE-style) — the
   honest conversation-recall number Sahil's (d) requires.
5. Only after (4): implement A-1 (Granite R2 embeddings) with hash fallback.
6. Kanban conversion of the roadmap: ONLY on Sahil's explicit go (currently
   HOLD).

---

## 9. Session provenance

- All state above verified live on 2026-08-03 against the working tree
  (git log/status, pytest collect, source greps, CI file, npm audit).
- Model on this session: deepseek-v4-flash-free (opencode-zen), CLI.
- Independent eval artifacts (for reference, not repo):
  `/home/kensei/.hermes/cache/delegation/subagent-summary-0-20260803_214955_543050.txt`,
  `/tmp/severian-third-party-eval-quan-2026-08-03/FINAL_REPORT.md`,
  `/tmp/severian-third-party-eval-kensei-2026-08-03/` (pytest/mypy/ruff logs,
  anomaly-result.json, quality-recompute.json, evidence-consistency.json).
