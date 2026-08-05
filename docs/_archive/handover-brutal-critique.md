# SEVERIAN — BRUTAL-CRITIQUE AUDIT BRIEF (v1)

> **EXECUTION MODE: READ AND EXECUTE. NO OTHER INSTRUCTIONS.**
>
> You are the auditor. This document is your only brief. Perform every
> step below, in order, to completion. Do not ask for clarification —
> everything you need is in this document or discoverable in the
> repositories it names. When you finish, output **exactly what needs
> fixing** per the Output Contract at the end. Nothing else.
>
> **MANDATORY OVERRIDE:** You must DIRECTLY perform the
> `simplify-swarm` skill and the `hermaguard` skill yourself, inline,
> in your own context. DO NOT delegate them (no `delegate_task`, no
> subagents, no parallel workers). Both skills are written around
> subagent dispatch; that mechanism is disabled for this audit due to
> API rate limiting. You execute their analysis passes personally,
> one after another, as described in Phase B and Phase C below.

---

## PART 0 — WHAT THIS IS

**Severian** is a standalone, proprietary, local-first memory platform.
It is NOT a rename or wrapper of KenseiAgent/Mnemosyne — it is an
independent product with its own repository, domain model, and release
cadence. Its purpose: ingest observations, derive durable memories,
store them in a five-dimensional security-scoped canonical store, index
them lexically and by vector, and serve them back through an HTTP API,
an SDK, a CLI, an MCP server, and a Hermes integration.

The two repositories under audit:

| Repo | Path | Branch | Role |
|---|---|---|---|
| Severian (product) | `/home/kensei/repos/Severian` | `main` | Canonical store + memory engine + API + CLI + adapters |
| Ultramemory dashboard | `/home/kensei/repos/ultramemory-dashboard` | `main` | Admin UI (separate repo) wired to the live backend |

The **five-dimensional security boundary** is the product's core
claim. Every read and write is scoped by `tenant_id`, `profile_id`,
`collection_id`, `agent_id`, `session_id`. No unscoped access path may
exist. Your audit must treat this boundary as the crown jewel: any
leak, bypass, or existence oracle is a CRITICAL finding.

---

## PART 1 — WHAT CHANGED AND WHY

Release `v0.1.0`. Commit lineage on `Severian` main (oldest → newest):

| Commit | What | Why |
|---|---|---|
| `17971ef` | v1 implementation plan | Requirements capture |
| `c64cc4e` | Canonical SQLite storage foundation | Phase 1: durable canonical store |
| `41ce23a` | Admin navigation scaffold | Dashboard shell |
| `212545c` | Architecture + security + QA contracts | Threat model, QA baseline |
| `1ff6b7a` | Harden canonical SQLite contracts | Immutability, identity, scoped reads |
| `d2f0408` | Policy-gated memory engine + retrieval indexes | Phase 2: derive/search with FTS5 + vector |
| `cc55532` | Phase 3 interfaces + benchmark contract | API/SDK/CLI/MCP/benchmark surface |
| `534b415` | MCP + Hermes adapter interfaces | Ecosystem integration |
| `9f6315f` | Backend-neutral contract + Postgres conformance target | Portability: `CanonicalRepository` protocol |
| `e0ac3d5` | **Live PostgreSQL/pgvector backend** | Real pgvector backend mirroring SQLite semantics |
| `2ac46de` | Release: runbook, CLI export/import, console script | Release artifact completeness |

**The three most consequential changes, and why they exist:**

1. **Derivation worker** (`src/severian/application/worker.py`).
   Previously, observations were ingested and enqueued as `derive`
   jobs but nothing drained the queue — search/graph/chat returned
   empty because memories were never promoted. The worker claims
   pending jobs, derives memories through `DerivationService`, rebuilds
   projections for touched scopes, and completes/fails jobs durably.
   Wired into `scripts/serve_dev.py` as a 0.2s drain loop. This closed
   the Phase 3 documented gap. **Audit the job lifecycle hard**:
   claim semantics, crash mid-derive, duplicate claim, unknown job
   kinds, projection rebuild failure.

2. **Recall-all path in retrieval** (`src/severian/application/retrieval.py`).
   The dashboard graph proxy sends `q=*`; FTS treats `*` as a literal
   and returns nothing, so the graph was empty. Fix: empty or `*`
   queries now list all memories in scope. **Audit the boundary hard**:
   does `*` recall-all leak across scopes? Does it bypass the
   lexical/vector fusion? Does it blow up on huge scopes (no LIMIT
   bypass)? Null-byte / control-char queries are stripped by the
   tokenizer — is that safe everywhere?

3. **Live PostgreSQL/pgvector backend**
   (`src/severian/infrastructure/postgresql.py` 786 lines,
   `src/severian/infrastructure/pgvector_index.py` 162 lines).
   Full mirror of `SQLiteRepository` semantics with SQLAlchemy 2 +
   psycopg3 + pgvector. Includes DSN normalisation
   (`postgresql://` → `postgresql+psycopg://`), a blank-scope
   fail-closed guard (`_require_scope`) added to BOTH backends
   (defence-in-depth: `model_copy` bypasses Pydantic construction
   validation), and a `reset()` helper for test fixtures. Proven by 42
   parametrized conformance tests + 3 live pgvector integration tests.
   **Audit the parity hard**: every SQLite path must have a Postgres
   twin with identical observable behaviour — idempotent identity
   inserts, scope predicates, revision chains, jobs, outbox, audit,
   export/import, backup.

---

## PART 2 — FILE MAP (READ EVERY FILE, EVERY LINE)

You must read and understand every file below. Do not skip the ones
that look boring. Boring files are where the leaks live.

### Severian — domain & contracts

| File | Lines | Role |
|---|---|---|
| `src/severian/domain/models.py` | — | Scope, Observation, Memory, Entity, Relation, Job, Event, AuditEvent — domain invariants (blank-scope rejection at construction) |
| `src/severian/backends.py` | 177 | `CanonicalRepository` + `ProjectionIndex` protocols; error aliasing (contract errors MUST be the same class across backends) |
| `src/severian/application/ports.py` | — | `Repository` protocol; worker/derivation seams |

### Severian — application (the logic you must verify end-to-end)

| File | Lines | Role |
|---|---|---|
| `src/severian/application/ingestion.py` | — | Ingest: normalize content, enqueue derive job |
| `src/severian/application/derivation.py` | — | Derive memories from observations via gateway + policy |
| `src/severian/application/worker.py` | — | `DerivationWorker.drain()` — claim, derive, project, complete |
| `src/severian/application/projections.py` | — | `ProjectionCoordinator` — FTS + vector upsert/rebuild per scope |
| `src/severian/application/retrieval.py` | — | `RetrievalService.search()` — recall-all path, fusion, `ContextAssembler` citations |
| `src/severian/application/service.py` | 275 | `MemoryService` — the composition root; API/CLI/SDK all funnel through it |

### Severian — infrastructure (the storage you must attack)

| File | Lines | Role |
|---|---|---|
| `src/severian/infrastructure/sqlite.py` | 679 | Reference implementation. Idempotent identity, immutability, revisions, jobs, outbox, audit, export/import, `_require_scope` guard |
| `src/severian/infrastructure/postgresql.py` | 786 | Live Postgres twin. **Parity audit required** |
| `src/severian/infrastructure/fts.py` | 123 | FTS5 lexical index |
| `src/severian/infrastructure/vector.py` | 131 | Local vector index (HashEmbedding) |
| `src/severian/infrastructure/pgvector_index.py` | 162 | pgvector vector index (`vector(256)`, cosine `<->`) |

### Severian — interfaces (the surfaces consumers touch)

| File | Lines | Role |
|---|---|---|
| `src/severian/api/app.py` | 253 | FastAPI v1 routes (`/healthz`, `/readyz`, `/observations`, `/search`) |
| `src/severian/api/schemas.py` | — | Pydantic strict models (`extra="forbid"`, nonempty scope, length caps) |
| `src/severian/sdk/client.py` | 232 | Typed HTTP SDK |
| `src/severian/cli/app.py` | 367 | Typer CLI: ingest/search/work/export/import + `[project.scripts]` console entry |
| `src/severian/mcp/server.py` | 531 | MCP server |
| `src/severian/integrations/hermes.py` | 509 | Hermes adapter |
| `src/severian/benchmarks/manifest.py` `runner.py` `cli.py` | 210/259 | Benchmark contract |
| `scripts/serve_dev.py` | — | Dev launcher: worker thread + uvicorn |

### Severian — tests (read them; they define the contract you verify)

| File | Role |
|---|---|
| `tests/contract/test_repository.py` + `test_repository_adversarial.py` | Contract + adversarial storage tests |
| `tests/contract/test_backends.py` | 13 conformance tests (both backends) |
| `tests/contract/test_backends_live.py` | 42 parametrized live conformance tests (SQLite + Postgres) |
| `tests/contract/test_pgvector_index_live.py` | 3 live pgvector full-stack tests |
| `tests/application/test_worker.py` | Worker drain/scope/failure regression + recall-all tests |
| `tests/integration/test_memory_engine.py` | Phase 2 integrations + adversarial regressions |
| `tests/integration/test_backup_export.py` | Backup/export/import roundtrips |
| `tests/api/test_api.py`, `tests/sdk/test_sdk.py`, `tests/cli/test_cli.py`, `tests/mcp/test_mcp_server.py`, `tests/integrations/test_hermes.py`, `tests/benchmarks/test_manifest.py`, `tests/unit/test_models.py` | Interface suites |

### Severian — docs (read them; the claims must match reality)

| File | Role |
|---|---|
| `docs/architecture.md` | Architecture claims |
| `docs/threat-model.md` | Security posture + **aspirational claims** (API keys, deletion confirmation) — flag any claim with no implementation |
| `docs/memory-engine.md` | Memory engine ops |
| `docs/phase3-interfaces.md` | API/SDK/CLI contract |
| `docs/postgresql-backend.md` | Postgres backend |
| `docs/operator-runbook.md` | Release ops |
| `docs/review-inline-2026-08-01.md` | Prior inline review — your job is to find what it MISSED |
| `docs/qa-e2e-blueprint.md`, `docs/provider-benchmark.md`, `docs/benchmarks/phase3-smoke.json`, `docs/plans/2026-07-31-severian-v1.md` | Plans/baselines |

### Dashboard (read every route + page + lib file)

`src/lib/severian.ts` (HTTP client), `src/lib/scope-context.tsx`
(5-dim scope context), `src/app/api/*` (server-side proxies: memories,
memories/[id], memories/graph, chat, documents, keys — **the proxy
default `q=*` is the known recall-all trigger**), `src/app/*` pages
(memories, memories/[id], graph, chat, import, keys, page), components
(Sidebar, ui), `globals.css`, `layout.tsx`. Dashboard committed at
`e229ecd`. **Audit the proxy chain for scope leakage and the keys page
for client-side-only claims.**

---

## PART 3 — ENVIRONMENT STATE (verified at audit start)

- Severian backend: runnable via
  `python scripts/serve_dev.py --host 127.0.0.1 --port 8000 --storage <path>`
  (NOT `uvicorn severian.api.app:app` — no module-level `app`).
- Postgres container: `pgvector/pgvector:pg16` on `127.0.0.1:54329`,
  DSN `postgresql://severian:[REDACTED]@127.0.0.1:54329/severian`
  (set `SEVERIAN_PG_DSN` for live tests; tests skip when unset).
- Dashboard dev server preferred port `3100` (port 3000 is Open
  WebUI — do NOT use it).
- Current known-good gate (your baseline; you must re-verify):
  - Severian: 197 tests pass, `ruff check` clean, `mypy --strict` clean.
  - Dashboard: `npm run lint` 0 errors, `npm run build` 13 routes.
- Tooling:
  - Severian: `uv run pytest -q`, `uv run ruff check src tests scripts`,
    `uv run mypy --strict src/severian`, `git diff --check`.
  - Dashboard: `npm run lint`, `npm run build`.

---

## PART 4 — THE AUDIT (execute in order, to completion)

### Phase A — Verify the implementation, logic, flows, pipelines, tool calls

A1. **Read everything.** Every file in Part 2, in full. Do not skim.
A2. **Trace every pipeline end-to-end** and name each tool call in the
    chain:
    - Ingest pipeline: API `POST /observations` → schema validation →
      `MemoryService.ingest_observation` → `IngestionService` → repo
      `add_observation` → `enqueue_job(derive)` → outbox event. Verify
      every hop, every argument, every error path.
    - Derive pipeline: `DerivationWorker.drain` → claim job →
      `DerivationService` → gateway `.derive()` → `add_memory` →
      `ProjectionCoordinator` upsert (FTS + vector) → `complete_job`.
    - Search pipeline: `POST /search` → `RetrievalService.search` →
      FTS query + vector query → fusion → `ContextAssembler` → scoped
      hits. Trace the `q=*` recall-all path.
    - Backup/restore: `export_json` → snapshot → `import_json` →
      rebuild. Trace checksum verification and conflict rejection.
    - Dashboard chain: page → API route (server-side proxy) → Severian
      HTTP → backend. Verify the proxy forwards scope correctly and
      never leaks cross-scope data.
A3. **Verify the five-dimensional scope boundary on EVERY read/write
    path.** For each repo method, each API route, each SDK method, each
    CLI command: is the scope mandatory? Is a blank dimension rejected?
    Can a caller observe existence of a record in another scope
    (existence oracle — 404 vs 200 differences, error message detail)?
A4. **Verify cross-backend parity.** For every method in
    `SQLiteRepository`, find the twin in `PostgreSQLBackend` and
    confirm identical observable behaviour: idempotent identity
    inserts, scope predicates, revision chains (superseded_by_id),
    job claim/complete/fail, outbox, audit, export/import roundtrip,
    backup snapshot. Flag ANY divergence.
A5. **Attack the tool calls.** For each external-ish call — SQL
    statements, FTS queries, HTTP routes, file writes, subprocess
    invocations, `curl`-style network in scripts — check injection
    surfaces, path traversal, unbounded queries, silent failure
    swallowing, and whether error details leak to untrusted clients.
A6. **Re-run the full gate and extend it.** Run the tooling commands
    in Part 3. Then write and run ADDITIONAL adversarial tests for any
    edge you find that the suite doesn't cover: cross-scope probing,
    idempotency under retry, tampered snapshots, huge recall-all
    scopes, concurrent worker claims, Postgres vs SQLite divergence.
    Every test you add must pass before you finish.

### Phase B — DIRECTLY perform `simplify-swarm` (inline, no delegation)

Load the skill with `skill_view(name='simplify-swarm')`. You WILL NOT
dispatch its three subagents. Instead, you personally perform its three
analysis passes in sequence over the full codebase (not just the diff —
this is a full-release audit):

1. **Hygiene pass** — dead code, AI slop (casts, redundant guards,
   comments that restate code), redundant abstractions (pass-through
   wrappers, single-use helpers), stale state, unreachable branches,
   duplicate definitions.
2. **Clarity pass** — duplication, naming, structural complexity,
   consistency, magic numbers, function length.
3. **Correctness pass** — N+1 queries, memory/resource leaks,
   concurrency issues (worker claim races, thread-safety in the dev
   launcher), leaky abstractions, silent failures, unguarded edge
   cases.

Consolidate your three passes yourself into a single tiered plan:
SAFE → CAREFUL → RISKY. **You are read-only during analysis.** After
Phase C completes, fold both skills' outputs into the final Fix List
(Phase D). You may apply SAFE/CAREFUL findings only if the fix is
unambiguous and the full gate still passes after each applied tier;
RISKY findings go into the Fix List unapplied with a recommended fix.

### Phase C — DIRECTLY perform `hermaguard` (inline, no delegation)

Load the skill with `skill_view(name='hermaguard')`. You WILL NOT
dispatch its three subagents. You personally perform its three
perspectives in sequence over the full codebase:

1. **Edge Case Hunter pass** — walk every branching path and boundary
   condition: null/empty/zero/NaN, off-by-one, state transitions,
   async races, degenerate handlers, empty catch/then blocks.
2. **Adversarial Reviewer pass** — attack surfaces: auth/permissions
   bypasses, data integrity, race conditions (TOCTOU), rollback
   safety, schema drift, swallowed errors, observability, input
   validation, return-value integrity. Cite specific file:line.
3. **Blast Radius pass** — for every changed/public symbol find all
   callers and callees; check API contracts, config coupling, database
   migrations, revert safety, performance anti-patterns.

Also run the deterministic pre-scan phase: `ruff check` and (if
available) `bandit`/`semgrep` over `src/severian`. Investigate every
deterministic hit for exploitability — do not just list it.

Triage everything into CRITICAL / HIGH / MEDIUM / LOW. CRITICAL =
data loss, scope/security boundary breach, auth bypass, unrecoverable
corruption. Escalate a finding when blast-radius confirms the affected
surface is externally reachable (API route without auth, SDK method
callable by any consumer).

**You are read-only. You apply NO fixes during this phase.**

### Phase D — Consolidate and output the Fix List

Merge Phase A findings, your simplify-swarm consolidation, and your
hermaguard report into ONE deterministic output per the contract
below. De-duplicate; keep the most specific version; note cross-pass
agreement (e.g. "found by Edge Case + Adversarial passes").

---

## PART 5 — OUTPUT CONTRACT

When you finish, output EXACTLY this structure. This is the deliverable
Sahil will act on. Be precise. Be specific. No filler.

```
# SEVERIAN AUDIT — FIX LIST

## GATE STATUS
- [ ] Severian pytest: <N passed / N failed — paste tail>
- [ ] ruff: <pass/fail>
- [ ] mypy --strict: <pass/fail>
- [ ] Dashboard lint: <pass/fail>
- [ ] Dashboard build: <pass/fail>
- [ ] Additional adversarial tests added: <N> — list names

## CRITICAL (must fix before any further release)
1. [severity] Title
   - File: path:line
   - What breaks (concrete scenario, not hypothetical)
   - Why vulnerable (cite code)
   - Fix: specific change

## HIGH
...

## MEDIUM
...

## LOW
...

## SCOPE-BOUNDARY VERDICT
PASS / FAIL — one paragraph on the five-dimensional boundary after
your probes (cross-scope leak, existence oracle, blank-scope guard,
recall-all boundary, Postgres parity).

## PARITY VERDICT (SQLite vs Postgres)
PASS / FAIL — list any divergence with file:line.

## THINGS THE PRIOR INLINE REVIEW MISSED
List every finding that `docs/review-inline-2026-08-01.md` did not
already disclose. This section is mandatory — if you found nothing,
state that explicitly.

## FIXES YOU APPLIED (Phase B)
- Applied (SAFE/CAREFUL, gate green after each): list
- Left unapplied (RISKY, in this list above): note
```

**Thoroughness directive:** Be relentless. Every "no findings" claim
must be earned by actually probing the path. If a claim in
`docs/threat-model.md` (API keys, deletion confirmation, auth) has no
implementation, flag it. If the dashboard keys page claims
client-side-only storage, verify it. If a test asserts something the
code doesn't actually do, say so. The prior review found the storage
solid; your job is to find what it missed. Output the truth, not a
clean bill of health.
