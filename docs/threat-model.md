# Severian Threat Model

## Protected assets

- original source content;
- derived memories and profiles;
- entities, relationships and temporal history;
- encryption and model credentials;
- deletion and retention state;
- exports and backups;
- provenance and audit records.

## Primary threats and required controls

| Threat | Required control |
|---|---|
| Cross-customer or cross-collection recall | Scope is mandatory in every repository/index query; deny missing scope; adversarial isolation tests |
| Cloud processing of prohibited data | Policy check before adapter invocation or network activity; fail closed; audited decision |
| Derived claim without evidence | Every active memory has source or explicit user-authored provenance; API returns citations |
| Correction destroys history | Immutable observations and revisions; supersession instead of overwrite |
| Deleted data remains searchable | Transactional deletion intent, projection tombstones, verification and rebuild tests |
| Index corruption or drift | Canonical/index revision tracking, lag metrics, repair and full rebuild |
| SQLite contention or partial writes | Controlled writer, WAL, short transactions, durable outbox and crash tests |
| Malicious source content | Treat source as data; never execute embedded instructions; sanitise renderers and filenames |
| Prompt injection into derivation | Fixed system contract, source delimiters, typed output validation and provenance |
| Backup leakage | Private permissions, encryption, checksums, scoped retention and restore audit |
| Export traversal or overwrite | Fixed export root, safe filenames, atomic create and overwrite refusal |
| Forged adapter identity | Authenticated client identity, scoped keys and audit correlation |
| Shared-control-plane compromise | Customer data planes and keys remain isolated; control plane stores no memory content |
| Dependency compromise | Lockfiles, hashes, vulnerability checks and minimal runtime images |

## Authentication and authorisation

**Implemented (SEC-01, 2026-08-05):** API-key authentication and
principal-bound scope are now implemented for the HTTP API.

- **API keys hashed at rest** with SHA-256. The credential store is a
  small SQLite database (``--auth-file`` / ``SEVERIAN_AUTH_FILE``). Keys
  are never persisted in plaintext; verification uses constant-time
  comparison. Keys are scoped to a principal :class:`Scope` and are
  revocable.
- **Fail-closed HTTP API.** When auth is enabled (a principal resolver is
  configured), every data endpoint requires a valid
  ``Authorization: Bearer <key>`` header. Missing or invalid tokens are
  rejected with 401. Cross-tenant claims are rejected with 403 (via the
  existing ``_bind_principal``). Health endpoints (``/healthz``,
  ``/readyz``) are exempt.
- **Non-loopback startup guard.** ``serve_dev.py`` refuses to start on a
  non-loopback host (not ``127.0.0.1``/``localhost``/``::1``) unless
  ``--auth-file`` is provided. This prevents accidental network exposure
  without authentication.
- **SDK credential support.** ``SeverianClient`` accepts ``api_key`` and
  sends ``Authorization: Bearer`` on every request.
- **MCP launch-time binding (fail closed).** The stdio entry point
  (`python -m severian.mcp`) refuses to start unless it can pin a
  tenant/profile principal from `--tenant-id`/`--profile-id` or
  `SEVERIAN_MCP_TENANT_ID`/`SEVERIAN_MCP_PROFILE_ID`, or unless the operator
  explicitly passes `--trusted-single-tenant`. The refusal names those exact
  flags. Tool arguments cannot switch the bound tenant/profile; a mismatch is
  a scope error. Only tenant and profile are pinned — `collection_id`,
  `agent_id` and `session_id` remain caller-chosen, so this is not an agent or
  session isolation boundary. The opt-out is trusted-context only and is not
  a way to run a shared or untrusted stdio server.
- **Hermes construct-time binding.** ``SeverianMemoryProvider`` optionally
  accepts a principal :class:`Scope` at construction. When bound,
  ``initialize()`` and tool calls cannot switch tenant/profile.

**Still required (not yet implemented):**
- browser sessions using secure cookies and CSRF protection;
- rate limiting (acceptable for local-first; add before network exposure).

**Delivered for one operation (M1):** destructive operations requiring explicit
confirmation + audit events — `severian purge` is dry-run by default, acts only with
`--confirm`, and writes one append-only audit row per target id. Every other
destructive path (erasure, supersede, close, retraction) already writes its own audit
rows (M2). No other destructive operation has a confirmation gate yet.

See `docs/operator-runbook.md` for the current deployment posture.

## Data lifecycle

Retention, expiry, legal hold, deletion and export operate on canonical IDs. Deletion states are explicit: requested, canonical-deleted, projections-purged and verified. Physical purging must not be claimed until canonical storage, every index and backup policy have been checked.

**Purge, as implemented (M1, 20/11/2026 — reverses the earlier "not offered" position on owner direction).** `severian purge --observation <id>` (dry run by default, `--confirm` to act) scrubs the observation's whole revision chain, every memory derived from it, the relations that lose their last support and the entity names nothing else supports. The states it actually reaches:

| State | Reached by purge? | What it means |
|---|---|---|
| requested | yes — the audit row (`observation.purge` et al.) is the request record | ids and counts only, never content |
| canonical-deleted *(purged)* | yes — text replaced by the fixed `[purged]` marker, digests recomputed over it, `identity_key` released, never an active head again | the row still exists as an audit skeleton: ids, scope, kinds, timestamps, revision links |
| projections-purged | yes — the derived memories are dropped from the FTS and vector indexes and the scope is rebuilt | on SQLite the index files are checkpointed and `VACUUM`ed afterwards |
| verified | yes — the acceptance test scans every SQLite store file (canonical, FTS, vector, and their `-wal`/`-shm` sidecars) for the marker bytes after checkpoint + `VACUUM` | the claim is per store file, not global |

**Not reached, and never claimed:**

- **Backup policy.** Purge cannot reach copies outside the store: `~/.hermes/backups/severian-*`, `~/backups/severian-*`, snapshot files produced by `severian export`, PostgreSQL base backups, or any copy the operator made. Deleting or rewriting those is a separate operator action.
- **PostgreSQL on-disk history.** The scrubbing UPDATE writes new row versions; the old ones remain as dead tuples until a `VACUUM`. An ordinary `VACUUM` does not rewrite pages in place — only `VACUUM FULL` rewrites the heap — and WAL archives may still hold old images. `severian purge` prints the operator command and does not run it.
- **A whole-host claim.** "Physically purged" is therefore never asserted. The accurate statement is "removed from this store and its index files, with the backups and the PostgreSQL caveats listed here".

## Phase 2 isolation gate

No customer activation until tests prove:

- tenant-less calls are rejected;
- IDs from one tenant cannot be read, mutated, searched, exported or deleted by another;
- indexes and caches preserve tenant scope;
- encryption keys and backups are independent;
- logs and metrics contain no raw memory content;
- migration and restore cannot cross customer boundaries.
