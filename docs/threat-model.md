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
- **MCP construct-time binding.** ``SeverianMCPServer`` optionally accepts
  a principal :class:`Scope` at construction. When bound, tool args cannot
  switch tenant/profile — the principal's security-sensitive dimensions
  override them. (stdio MCP cannot do interactive auth; capability binding
  means the server's scope is fixed at construction.)
- **Hermes construct-time binding.** ``SeverianMemoryProvider`` optionally
  accepts a principal :class:`Scope` at construction. When bound,
  ``initialize()`` and tool calls cannot switch tenant/profile.

**Still required (not yet implemented):**
- browser sessions using secure cookies and CSRF protection;
- destructive operations requiring explicit confirmation + audit events;
- rate limiting (acceptable for local-first; add before network exposure).

See `docs/operator-runbook.md` for the current deployment posture.

## Data lifecycle

Retention, expiry, legal hold, deletion and export operate on canonical IDs. Deletion states are explicit: requested, canonical-deleted, projections-purged and verified. Physical purging must not be claimed until canonical storage, every index and backup policy have been checked.

## Phase 2 isolation gate

No customer activation until tests prove:

- tenant-less calls are rejected;
- IDs from one tenant cannot be read, mutated, searched, exported or deleted by another;
- indexes and caches preserve tenant scope;
- encryption keys and backups are independent;
- logs and metrics contain no raw memory content;
- migration and restore cannot cross customer boundaries.
