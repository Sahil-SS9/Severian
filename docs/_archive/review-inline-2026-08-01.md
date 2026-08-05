# QA / Security / E2E Review — inline pass (2026-08-01)

Per Sahil's instruction (sub-agent reviews unavailable due to API rate
limiting), this review was performed lightly inline by Kensei acting as
its own skeptical reviewer: attack the security boundary, scope
isolation, idempotency, and E2E paths from outside the happy path.
The in-depth version of this review is embedded in the brutal-critique
handover prompt (`docs/handover-brutal-critique.md`) for third-party
verification.

## Verification environment

- Severian backend (Postgres + pgvector container) live on
  `127.0.0.1:8021` via `scripts/serve_dev.py`.
- `pgvector/pgvector:pg16` container `severian-pg-test` on
  `127.0.0.1:54329`.
- Dashboard proxy chain on `127.0.0.1:3100` (serving the live backend).
- Full gate: 195 passed (150 baseline + 42 backend conformance + 3
  pgvector integration); `ruff check` clean; `mypy --strict` clean;
  `git diff --check` clean; working trees clean on both repos.

## Findings

### PASS — Scope isolation (core security boundary)

| Probe | Result |
|---|---|
| Ingest under scope A (alice/main/sess-1) | 201, observation + derive job |
| Worker promotes observation → memory | Search finds it without `derive` flag |
| Search same scope | 1 hit, correct content |
| Search different profile (bob) | 0 hits — no leak |
| Search different tenant | 0 hits — no leak |
| GET observation cross-scope | 404 — no existence oracle |
| GET observation same scope | 200 |
| Recall-all graph query (`*`) | returns all in-scope memories |
| Blank scope dimension | 422 (rejected at schema AND backend guard) |
| Extra/unknown field | 422 (`extra="forbid"` strict schema) |
| Oversized query (>10k chars) | 422 (length-capped) |
| Null-byte in query | 200, tokenizer strips non-word chars — no injection, no crash |

Scope is enforced at three layers: Pydantic construction validation,
backend `_require_scope` fail-closed guard (both SQLite and Postgres),
and five-column predicates on every query. Verified working over the
live HTTP API.

### PASS — Idempotency and immutability

- Re-ingesting identical content returns the same observation id and
  does not duplicate records (verified live).
- Observations are immutable; revisions append new rows and set
  `superseded_by_id` (conformance suite, both backends).
- Entity/relation identity keys are casefolded for stable identity.

### PASS — Cross-backend conformance

- 42 parametrized conformance tests run the same runtime semantics
  against SQLite and live Postgres; both backends produce identical
  observable results (roundtrip, isolation, idempotency, revision
  chains, job lifecycle, outbox/audit, export/import roundtrip,
  checksum tamper rejection, backup snapshot).
- The conformance suite caught and fixed three real bugs during the
  build: Postgres raising a different `NotFoundError` class than the
  contract aliases; JSONB vs JSON column type for `.astext` predicates;
  missing blank-scope guard in the reference SQLite backend (contract
  defence-in-depth violation).

### PASS — Hygiene

- No credentials/keys/tokens in source, config, tests, or scripts
  (grep scan clean; dev credentials only in ephemeral container).
- No `breakpoint()`/`pdb`/debug prints in `src/severian`.
- No TODO/FIXME/XXX/HACK markers in `src/severian`.
- Dashboard proxy chain (search, graph) live and returning 200.
- Dashboard: `npm run lint` 0 errors, `npm run build` 13 routes pass,
  committed `e229ecd`.

### WARN / gaps to disclose in the brutal-critique handover

1. **No authentication on the HTTP API.** The API is loopback-bound by
   default in dev, and the threat model documents local auth as a Phase
   1 requirement — but there is no API-key layer yet. The threat model
   claims "API keys are hashed at rest, scoped and revocable" which is
   aspirational, not built. Anyone with host access can read the store.
   Not a leak within the documented posture; a real gap for the product
   claim.
2. **No CORS middleware.** Deliberate (dashboard proxies server-side to
   avoid CORS), but any non-proxied browser client cannot call the API
   directly. Documented, not a defect.
3. **CLI lacks export/import commands.** `export_json`/`import_json`
   exist on the repository contract and are conformance-tested, but the
   CLI exposes only `ingest`, `search`, `work`. An operator wanting a
   portable snapshot must write a small Python snippet or use
   `backup()`.
4. **No `[project.scripts]` console entry.** `severian` CLI is reachable
   only via `python -m severian.cli.app`; no installed binary. Affects
   the release artifact packaging story.
5. **Backup semantics differ across backends.** SQLite `backup()`
   writes a binary DB file; Postgres `backup()` writes a JSON export
   doc. Both are restorable, but an operator must know which format
   they hold. Not a correctness defect; a documentation/UX gap.
6. **FTS tokenizer accepts null bytes / control characters in queries**
   (strips them safely, no injection). Hardening opportunity: reject
   control chars at the schema layer.
7. **No rate limiting / abuse protection on the API.** Fine for
   local-first; must be addressed before any network exposure.
8. **No destructive-operation confirmation.** Threat model requires
   explicit confirmation + audit for destructive ops; revision
   supersession exists but there is no delete endpoint in the current
   API slice.

### Known non-goals (excluded from scope by Sahil)

- Dashboard UI/UX revisit (explicitly excluded from this review scope).
- Release artifact packaging (item 5, in progress separately).

## Verdict

Within the documented local-first posture, the implementation holds the
five-dimensional security boundary and cross-backend contract. The
gaps above are disclosure items for the brutal critique — none of them
are silent defects that would mislead a reviewer into believing auth,
CORS, rate limiting, or deletion confirmation exist when they do not.
