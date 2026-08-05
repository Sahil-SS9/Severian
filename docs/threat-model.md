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

**Required but not yet implemented (2026-08-05):** authentication, API keys,
and CSRF protection are *required* before any network exposure but are NOT
implemented in the current codebase. The operator runbook documents this:
the HTTP API is loopback-only by default and has no authentication, no API
key validation, and no CSRF protection. Any host user can read the store.

Phase 1 requires:
- local authentication (loopback binding is not authentication);
- API keys hashed at rest, scoped and revocable;
- browser sessions using secure cookies and CSRF protection;
- destructive operations requiring explicit confirmation + audit events.

None of these are shipped yet. Do not expose the API on a network interface
until they are implemented. See `docs/operator-runbook.md` for the current
deployment posture.

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
