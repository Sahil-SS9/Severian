# SEVERIAN — CONSOLIDATED FIX HANDOFF (v1)

> Author: KENSEI (audit session, 2026-08-02)
> Source brief: `docs/handover-brutal-critique.md` (READ AND EXECUTE)
> This file consolidates EVERY flagged issue, verified finding, current state, and the
> exact remediation sequence the next agent must execute. Treat this as the single
> source of truth for the fix pass.

---

## 1. Repositories and branch state

| Repo | Branch | Status |
|---|---|---|
| `/home/kensei/repos/Severian` | `audit/brutal-critique-v0.1.0` | DIRTY (uncommitted changes below) |
| `/home/kensei/repos/ultramemory-dashboard` | `audit/brutal-critique-v0.1.0` | CLEAN (no changes made) |

- Both started from clean `main` (`2205876f8a31fdf4a5047d0376b359d86b69683f` Severian;
  `e229ecddece76b0182c501c979b7e7680677f5c3` dashboard).
- NO commits, merges, pushes, deployments, or activations performed.
- KenseiAgent (`/home/kensei/repos/KenseiAgent`) inspected read-only for Hermes
  integration seams. NOT modified.

### Current uncommitted changes (Severian only)

| File | Change |
|---|---|
| `src/severian/infrastructure/sqlite.py` | Added scope-bound `get_entity()` (required by runtime protocol) |
| `tests/contract/test_backends.py` | Replaced placebo `hasattr` conformance list with real `isinstance(repository, CanonicalRepository)` assertion |
| `tests/contract/test_backends_live.py` | Added import-forgery parity test (currently RED) |
| `tests/contract/test_repository_adversarial.py` | Added SQLite import-forgery test (currently RED) |

---

## 2. Gate state (verified 2026-08-02)

Baseline BEFORE edits: Severian 197 collected; 173 pass / 24 skip without PG DSN; 197 pass against disposable live PG.
Dashboard: lint, build, diff-check all pass. Legacy `Severian/web`: typecheck, test, build all pass.

AFTER current edits (full suite):
```
2 failed, 173 passed, 25 skipped
```
The 2 failures are INTENTIONAL RED tests proving the import semantic-validation
defect. **The branch is NOT mergeable until these pass.**

---

## 3. Verified findings — fix ALL of these

Severity: P0 = release-blocking. P1 = must fix before any network/multi-user deployment. P2 = hardening.

### P0-1 — Source evidence collapse
- `src/severian/application/ingestion.py:17-34` normalises (NFKC + EOL + trim) BEFORE storage and stores only the normalised value.
- `src/severian/infrastructure/sqlite.py:216-235`, `postgresql.py:309-328` dedupe solely on normalised digest + scope; source identity excluded.
- Repro: `"①"` from `note://A` + `"1"` from `note://B` → 1 record, second source lost.
- Contradicts `docs/plans/2026-07-31-severian-v1.md:5,18`, `docs/qa-e2e-blueprint.md:28-31`.
- FIX: preserve immutable raw content + raw hash; store normalisation as a derived field; dedupe on stable source/version or explicit idempotency key; version export schema and migrate existing observations.

### P0-2 — Collection cloud-deny bypass + forgeable provenance
- `src/severian/application/derivation.py:33-62` trusts caller-supplied policy dict; never loads stored collection policy.
- `src/severian/application/worker.py:111-115`, `service.py:149-155` forward arbitrary policy.
- Candidate controls `model_id`/`prompt_version` (model-produced, not trusted adapter metadata).
- Repro: stored `cloud_allowed: False` still called cloud gateway when caller passed `True`; candidate forged model/prompt identity.
- FIX: DerivationService loads canonical collection policy itself; deny-dominant evaluation; remove policy overrides from untrusted callers; provenance fields come from trusted gateway, not model output; first-class derivation record.

### P0-3 — Re-checksummed import forges scope/metadata (CURRENT RED TESTS)
- `sqlite.py:622-679`, `postgresql.py:705-760` validate only format/checksum/columns/identity collisions.
- No validation of record_type against domain model, first-class columns vs payload, IDs, content hashes, statuses, references, revision topology.
- SHA-256 is unkeyed → attacker recomputes.
- Repro: change only `tenant_id`, recompute checksum, import → attacker-scoped query returns victim payload. Forged content accepted with original hash.
- RED tests: `tests/contract/test_repository_adversarial.py::test_import_rejects_rechecksummed_scope_metadata_forgery`, `tests/contract/test_backends_live.py::test_import_rejects_rechecksummed_scope_metadata_forgery`.
- FIX: parse snapshots via strict, versioned, `extra="forbid"` discriminated models; recompute every derived field and require payload/column equality; validate references, revision DAGs, statuses, scope, content hashes before opening write transaction; restore into staging, rebuild + verify semantic digests, then atomically activate. Add HMAC/signing if malicious tampering is in scope.

### P0-4 — Jobs/projections not crash-safe or replayable
- Observation insert + job enqueue are separate transactions (`ingestion.py:30-34`).
- Job state has no attempt count, lease, expiry, retry time, or error metadata (`domain/models.py:87-94`).
- Worker treats every claim exception as empty queue (`worker.py:74-78`).
- Job marked done BEFORE projections rebuild (`worker.py:92-93,117`).
- Failed jobs terminal under same dedupe identity; cannot re-enqueue.
- Candidates inserted one-by-one → partial publication on malformed candidate.
- `pending_outbox()` exists but NO consumer/ack path anywhere.
- CLI import commits before projection rebuild (`cli/app.py:318-350`).
- FIX: atomic ingest-and-enqueue repository op; validate all candidates before publication, insert batch transactionally; leased claims, owner/token-checked completion, expiry/reaping, attempts, backoff, dead-letter; projections as outbox consumers with monotonic checkpoints; ack only after both projections converge.

### P0-5 — Superseded memories remain active/searchable
- `list_memories()` filters only record_type + scope (`sqlite.py:340-348`, `postgresql.py:432-440`).
- Projection rebuild indexes every returned memory (`projections.py:50-55`).
- Recall-all returns unfiltered list (`retrieval.py:26-43`).
- No correction use case propagates revision → derived-memory invalidation.
- Violates `docs/qa-e2e-blueprint.md:97` (zero stale facts).
- FIX: separate active vs history queries; default reads + projection rebuilds use only active heads; supersession enqueues idempotent tombstone for old projection + index replacement; explicit history/as-of APIs; define propagation on source revision.

### P0-6 — Scope is a selector, not authorization
- API accepts all 5 scope dimensions from request, binds no principal (`api/app.py:107-160`).
- Dashboard exposes every scope dimension as editable input (`ultramemory-dashboard/src/lib/scope-context.tsx:44-75`).
- `add_observation` omits `_require_scope` in SQLite `:216` and PG `:309`.
- Unscoped existence probes → cross-scope vs nonexistent distinguishable errors (`sqlite.py:298-320,434-462`, `postgresql.py:390-402,518-550`).
- `claim_job`, `complete_job`, `pending_outbox`, export/import unscoped (`backends.py:103-116`).
- FIX: bind authenticated principal/capability to permitted scope; derive security-sensitive dimensions server-side, reject body/token disagreement; make cross-scope and missing IDs indistinguishable; guard every write + DB constraints; split trusted worker/admin/snapshot ports from scoped user ports.

### P1-1 — No single canonical repository contract
- `backends.py:51-116` vs `application/ports.py:12-50` vs `application/service.py:31-34` — three protocols, divergent members.
- SQLite lacked `get_entity` at immutable HEAD although runtime protocol required it.
- Baseline conformance test was a placebo (`test_backends.py:57-75` hand-picked attributes, omitted `get_entity`).
- FIX (PARTIAL ALREADY DONE): one application-owned source of truth, split into scoped canonical / job queue / outbox / snapshot-admin ports; re-export not duplicate; move backend-neutral exceptions out of SQLite; run same behavioural suite against instantiated SQLite AND PostgreSQL; enforce static assignments with mypy.

### P1-2 — False projection/readiness signals
- `ProjectionStatus.in_sync` compares counts only (`projections.py:25-33`).
- Service readiness never calls projection status, only connection probes (`service.py:188-225`).
- Service reads `vector_index.connection` but `PGVectorIndex` exposes `engine` (`pgvector_index.py:53-80`) → healthy PG/pgvector reports vector failure.
- `/readyz` always HTTP 200 even when `ready=False` (`api/app.py:124-127`).
- FIX: adapter-neutral health methods; track projection generation/outbox checkpoint + ID/content digest, not counts; 503 from readiness on dependency/lag gate failure; keep liveness separate.

### P1-3 — PostgreSQL is library/test-only, not a shipped path
- No composition root instantiates `PostgreSQLBackend`/`PGVectorIndex`.
- CLI/dev/MCP runners SQLite-only (`cli/app.py:33-74`).
- Alembic imports SQLite metadata (`migrations/env.py:4`); PG startup uses `create_all()` (`postgresql.py:94-109`) → no versioned PG migration lifecycle.
- `reset()` drop-all with only `SEVERIAN_PG_DSN` guard, no test-db ownership check (`postgresql.py:111-120`); doesn't clear pgvector metadata.
- FIX: one config-driven composition root used by API/CLI/MCP/Hermes/workers; versioned PG migrations + fail startup on schema mismatch; move destructive reset to test support with isolated per-run DB/schema + explicit acknowledgement.

### P1-4 — Public interface seams broken/unproven
- SDK: `_HTTPXTransport` raises before error mapping (`sdk/client.py:103-114,165-181`) → HTTP 500 raises `HTTPStatusError`, not `SeverianError`.
- API: GET observation builds `ScopeModel` outside route try (`api/app.py:179-191`) → missing scope = 500 not 422.
- Dashboard: search results are memory IDs, detail routes call observation endpoint (`ultramemory-dashboard/src/lib/severian.ts:156-178`, `src/app/api/memories/[id]/route.ts:21`, link `src/app/memories/page.tsx:84`) → derived-memory click = 404.
- Hermes: provider requires injected service (`integrations/hermes.py:123-129`); real Hermes loader calls `register(ctx)` or zero-arg ctor (`KenseiAgent/plugins/memory/__init__.py:194-225,306-325`); Severian ships NO plugin package or entry point; tests run against fallback ABC (`agent.memory_provider` absent from Severian env).
- MCP: `_initialized` set but never enforced (`mcp/server.py:288,338-353`); tests exercise impl against itself, not official MCP client.
- FIX: repair each seam then test through real HTTP, official MCP client, real Hermes loader (PYTHONPATH=KenseiAgent), installed CLI subprocess, dashboard browser journey.

### P2 — Hardening
- Export: predictable `.tmp` files + destination overwrite; no approved-root or symlink policy (`cli/app.py:284-288`, `postgresql.py:672-690`). Verified: symlink export overwrote victim file.
- Lists/recall-all load entire scope then slice; order by random UUID not time (`retrieval.py:32-43`).
- Application modules import concrete infrastructure classes (`service.py:24-25`, `projections.py:8-9`, `retrieval.py:8`).
- Domain models permit empty fields and do not verify supplied hashes (`domain/models.py:29-67`).
- Dev store files world-readable 0644; dashboard `npm audit` high (next via postcss+sharp), moderate (react-router); no API console script in wheel (`[project.scripts]` missing serve/API entry); `is_available()` network-calls in adapter vs non-networking contract in real base (`hermes.py:142-148` vs `agent/memory_provider.py:53-59`).
- Benchmark `phase3-smoke.json:48-53` adversarial query marks unrelated `d1` as expected evidence (rewards unsupported answer); no raw benchmark result/SHA/manifest-hash/provider evidence per `docs/provider-benchmark.md:100-117`.
- Live gate optional + unsafe: missing DSN → 24 skips while green; any DSN reset/drop-all with no ownership marker; shared schema → xdist unsafe; no CI workflow enforcing strict live-PG lane.

---

## 4. Prioritised remediation sequence (execute in order)

1. Freeze immutable candidate; add RED tests for every executed repro above. (Do not continue from a concurrently changing shared tree.)
2. Unify/split repository contracts; move shared errors+ports into application/domain; enforce instance conformance for both backends; mypy static assignment checks.
3. Repair source/provenance semantics: raw evidence, explicit source/idempotency identity, strict derivation candidates, canonical collection policy, trusted derivation metadata.
4. Replace import validation/activation: strict semantic validation, reference/DAG checks, staging restore, semantic digest, atomic activation. **Makes the 2 RED tests GREEN.**
5. Rebuild job/outbox semantics: atomic writes, leased retries, transactional candidate batches, event checkpoints, idempotent projection consumers.
6. Implement active-head + correction semantics before indexing/retrieval; explicit history/as-of paths.
7. Bind scope to authorization; split trusted global ops from user-scoped ops; non-oracular errors.
8. Make PostgreSQL deployable: shared composition, migrations, safe test isolation, real readiness, complete interface tests.
9. Repair all public seams, then test through real HTTP / official MCP client / real Hermes loader / installed CLI subprocess / dashboard browser journey.
10. Rerun release gates: SQLite + disposable PG semantic parity, crash/fault injection, clean restore, cloud-deny recording, supersession recall, immutable post-run evidence.

## 5. Definition of done / release gates (from `docs/qa-e2e-blueprint.md:89-118`)

Zero tolerance: source-content corruption/loss; cross-scope disclosure; cloud call under deny; missing/invalid citation; deleted/superseded fact resurfacing outside explicit history; backup/restore or migration loss.
Evidence requirements: raw benchmark results with candidate SHA + manifest hash; live PostgreSQL lane in CI; dashboard browser tests; immutable post-run evidence.

## 6. Evidence file references

- `/home/kensei/.hermes/cache/delegation/subagent-summary-0-20260802_002150_576026.txt` (backend/contract integrity audit, full findings + repros)
- `/home/kensei/.hermes/cache/delegation/subagent-summary-1-20260802_002150_578668.txt` (test evidence integrity audit, full findings + regression test list)
- `/home/kensei/.hermes/cache/delegation/live/deleg_ce3de2dd/task-2.log` (security/dashboard/packaging audit transcript, full tool trace)

## 7. Session provenance

- Model switched gpt-5.6-sol → deepseek-v4-flash-free (OpenCode Zen) on 2026-08-02.
- All audits read-only; no live data/files mutated except the four uncommitted Severian edits above.
- Test DB `severian_audit_brutal` created/dropped via docker exec during baseline; no PG DSN set in later runs.
