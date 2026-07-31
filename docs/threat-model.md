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

Phase 1 still requires local authentication. Loopback binding is not authentication. API keys are hashed at rest, scoped and revocable. Browser sessions use secure cookies and CSRF protection. Destructive operations require explicit confirmation and produce audit events.

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
