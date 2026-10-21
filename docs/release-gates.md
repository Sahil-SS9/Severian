## Severian Independent QA / E2E Blueprint

### 1. Test architecture

Run QA against a packaged release candidate—not developer internals—using disposable environments:

- **Backends:** SQLite/FTS5 and PostgreSQL/pgvector.
- **Entry surfaces:** HTTP API, Python SDK, CLI subprocess, MCP client, Hermes MemoryProvider, admin UI.
- **Deployment modes:** native clean install and Docker package.
- **Model fixtures:**
  - deterministic local derivation model;
  - recording cloud adapter that fails the test if invoked under deny policy;
  - fault-injecting adapter for timeout, malformed output, and partial failure.
- **Reference corpus:** version-controlled, synthetic, privacy-safe dataset containing:
  - exact quotations and structured metadata;
  - corrections and contradictory facts;
  - facts valid over bounded time ranges;
  - identical terms in different scopes;
  - deletion chains and derived memories;
  - attachments and Unicode/large-content cases.
- Maintain a **golden semantic manifest** independently of application output. It defines expected canonical records, lineage, temporal state, visibility, and recall—not database row layouts or ranking implementation details.
- All mutating tests use process-owned temporary roots/databases. Never point destructive fixtures at a live Severian or Hermes home.

### 2. Executable test matrix

| ID | Area | Executable scenario and assertions |
|---|---|---|
| **FID-01** | Source fidelity | Ingest byte-sensitive text, Unicode, multiline text, structured metadata, source timestamps, and attachment hashes through each write-capable interface. Export and retrieve it. Assert original content/hash/metadata are exact and distinct from normalized or derived content. |
| **FID-02** | Idempotency | Submit the same source repeatedly, concurrently, and with the same idempotency key through API/CLI/MCP. Assert one canonical observation, stable UUID, deterministic response, and no duplicate derivation jobs. |
| **PROV-01** | Provenance completeness | Derive multiple memories from one and multiple sources. Assert every memory records source IDs, model identity/version, prompt version, confidence, derivation timestamp, scope, and creation audit event. Reject lineage with missing or inaccessible sources. |
| **PROV-02** | Citation validity | Execute recall over the reference corpus. Resolve every returned citation to immutable source evidence and verify the cited source supports the result. No orphan, cross-scope, or deleted citation may appear. |
| **COR-01** | Correction | Correct an observation. Assert original evidence is unchanged; a revision is added; latest recall uses the correction; historical/as-of recall can still expose the prior state; provenance timeline remains complete. |
| **COR-02** | Supersession | Supersede a derived memory with a replacement. Assert default recall excludes stale content, explicit history returns the chain, cycles/self-links are rejected, and concurrent supersessions produce one valid visible head or a conflict. |
| **TMP-01** | Temporal recall | Seed “role=A” valid until T1 and “role=B” from T1. Query before, at, and after T1. Assert boundary semantics are consistent across both databases and all read interfaces. |
| **TMP-02** | Late correction | Backdate a correction after indexes already contain the old fact. Assert present-time and as-of retrieval are correct after projection repair/rebuild. |
| **SCP-01** | Scope isolation | Seed identical and unique markers across tenant/profile/collection/agent/session scopes. Query each allowed scope and all combinations. Assert zero forbidden IDs, snippets, counts, facets, citations, timing-independent autocomplete leakage, or context leakage. |
| **SCP-02** | Adversarial scope input | Attempt UUID substitution, omitted scope, conflicting token/body scope, encoded filters, batch mixed-scope IDs, and direct-object access. Require deny-before-fetch and an audit event without sensitive payload. |
| **DEL-01** | Deletion propagation | Delete a source with several memories, entities, relations, lexical/vector entries, caches, and queued jobs. Assert the documented retention policy in canonical history and zero active recall/context/index results after bounded propagation. |
| **DEL-02** | Delete under failure | Inject failure after canonical deletion but before index cleanup. Restart workers. Assert durable outbox replay converges, no resurrection occurs, and repeated delete is safe. |
| **DEL-03** | Backup race | Delete while a backup is taken. Restored state must correspond to a valid transaction boundary—never a torn source/index/audit state. |
| **IDX-01** | Index destruction/rebuild | Delete lexical and vector projections while preserving canonical storage. Rebuild from empty. Compare normalized search result IDs, citations, tombstones, and index generation metadata with the pre-destruction golden snapshot. |
| **IDX-02** | Corruption/lag repair | Corrupt or omit derived index records and simulate interrupted rebuilds. Readiness must report degraded state; repair must be resumable and idempotent; unavailable-index responses must follow the documented contract. |
| **BKR-01** | Clean-machine restore | Create backup, copy only documented artifacts to a fresh disposable machine/container, restore, and compare canonical semantic digest, attachments, audit history, UUIDs, scopes, and recall corpus. No original absolute paths may be required. |
| **BKR-02** | Backup integrity | Truncate, alter, mismatch, or remove backup components. Restore must fail before replacing active state and must leave the target unchanged. Validate manifest checksums and schema/version compatibility. |
| **CLD-01** | Cloud-deny | Enable collection-level cloud deny and exercise ingestion, derivation, repair, retry, fallback, reindex, admin actions, MCP, and Hermes paths. A recording network/cloud adapter must observe **zero calls**. No “fallback to cloud” is permitted. |
| **CLD-02** | Network deny | Run the complete local-only suite with outbound networking blocked. Core ingestion, derivation, recall, export, backup, and rebuild must pass. |
| **PAR-01** | Interface parity | Replay one operation manifest through API, SDK, CLI, MCP, and Hermes: create, duplicate ingest, recall, temporal recall, correct, supersede, delete, and errors. Normalize transport-only fields and require identical domain outcomes, UUIDs, visibility, citations, status class, and audit effects. |
| **PAR-02** | Canonical write path | Instrument application services/repositories. Assert no adapter or UI endpoint writes repositories/indexes directly; each mutation creates exactly one expected canonical transaction and audit trail. |
| **UI-01** | Admin journey | Playwright: authenticate → ingest → inspect source → inspect provenance/revision timeline → search/context preview → correct → verify changed recall → delete → verify disappearance and audit event. |
| **UI-02** | Destructive UI safety | Delete/restore/import actions require explicit confirmation, identify affected scope, resist double-submit, show terminal job status, and never report success before backend completion. Cancel must produce no mutation. |
| **UI-03** | Accessibility/security | Automated accessibility scan plus keyboard-only critical journey. Verify escaped source content, CSRF/session protections, secure cookie behavior, auth expiry, no secrets in DOM/logs, and no sensitive content in browser storage. |
| **DB-01** | Repository conformance | Parameterize one black-box repository contract suite over SQLite and PostgreSQL. Compare observable semantics for UUIDs, constraints, ordering, pagination, nulls, transactions, temporal boundaries, conflicts, revisions, and deletes—not SQL or row ordering accidents. |
| **DB-02** | Cross-backend semantic digest | Execute the same seeded operation journal on both backends. Export canonical normalized manifests and require exact semantic equality. Allow only explicitly documented backend metadata differences. |
| **MIG-01** | Schema migration | For every supported prior schema, restore fixture → migrate to current → run integrity/conformance/recall tests. Re-running migrations must be safe. Unsupported future schemas must fail closed. |
| **MIG-02** | SQLite→PostgreSQL | Migrate full corpus with revisions, tombstones, audit events, jobs, and attachments. Compare semantic digests and held-out recalls; stable application UUIDs must remain unchanged. |
| **MIG-03** | Cutover/rollback | Dry run → shadow comparison → quiesce writes → final sync → switch → write/read smoke test → rollback → verify original remains authoritative. Repeat with injected failure at each stage and ensure resumability/no double import. |
| **CON-01** | Concurrent ingestion | Many workers ingest the same and different observations. Require no duplicates, lost writes, leaked scopes, lock corruption, or stuck jobs; retries preserve idempotency. |
| **CON-02** | Conflicting mutations | Race correction/correction, correction/delete, recall/delete, rebuild/ingest, backup/write, and worker retries. Outcomes must match a documented conflict policy and preserve invariants. |
| **CON-03** | Soak/restart | Run representative mixed workload while repeatedly terminating service/workers at fault points. Restart and drain. Assert database integrity, outbox convergence, no permanent index lag, and no orphaned “running” jobs. |
| **SEC-01** | Auth/authz | Test missing, invalid, expired, and wrong-scope credentials on every interface and websocket/stream if present. Verify deny-by-default and consistent response policy. |
| **SEC-02** | Injection/content safety | SQL/meta-character, FTS query, JSON, path traversal, command-like source, prompt injection, oversized content, XSS, and malicious import/archive fixtures. Content must remain data; no execution or scope bypass. |
| **SEC-03** | Secret/privacy hygiene | Scan package, images, logs, metrics, traces, exports, backups, errors, and audit records for credentials or unintended memory contents. Metrics must not expose source text. |
| **SEC-04** | Filesystem boundaries | Backup/import/export/attachments must reject symlinks, traversal, overwrite, unsafe archives, and paths outside approved roots. Partial failures leave no published residue. |
| **SEC-05** | Dependency/container posture | Lockfile audit, SAST, secret scan, SBOM, container vulnerability scan, non-root container assertion, minimal exposed ports, and read-only filesystem where supported. |

### 3. Retrieval benchmark

Use a held-out corpus never used to tune ranking:

- Queries grouped by exact fact, paraphrase, multi-source synthesis, correction, temporal/as-of, negative/no-answer, and adversarial cross-scope cases.
- Score:
  - Recall@K and MRR/nDCG for expected evidence;
  - citation precision;
  - stale-fact rate;
  - temporal correctness;
  - cross-scope leakage;
  - unsupported-answer rate;
  - deterministic context-budget compliance.
- Store corpus version, seed, configuration, model/prompt identity, candidate commit, backend, and raw result manifest with every run.

### 4. Execution lanes

1. **Per commit:** unit, repository contracts, API/SDK/CLI contracts, frontend component tests, lint/type checks.
2. **Per pull request:** SQLite integration, interface parity, cloud-deny, focused security tests, Playwright critical journeys.
3. **Nightly:** PostgreSQL conformance, full backend semantic comparison, concurrency, index destruction/rebuild, migration matrix.
4. **Release candidate:** clean install/package tests, all E2E tests, outbound-network-denied run, retrieval benchmark, two independent backup/restore rehearsals, two migration/rollback rehearsals, soak/restart, full security scans.
5. **Independent QA rerun:** QA checks out the immutable candidate SHA and independently runs collection and execution without relying on developer summaries. Evidence must include commands, unmasked exit codes, runtime/backend versions, logs/artifacts, and post-run repository status.

### 5. Release thresholds

**Absolute blockers—zero tolerance:**

- Any source-content corruption or loss.
- Any cross-scope/customer disclosure.
- Any cloud call while deny policy is active.
- Any missing/invalid citation for a returned memory.
- Any deleted or superseded fact resurfacing outside explicit history.
- Any backup/restore or migration semantic mismatch.
- Any critical/high exploitable security finding.
- Any adapter bypassing the canonical application write path.
- Any unexplained SQLite/PostgreSQL behavioral divergence.
- Any flaky failure in isolation, deletion, migration, backup, or security tests.

**Required quantitative gates:**

- 100% pass for unit, contract, integration, E2E, migration, and security suites.
- 100% semantic digest equality across backup/restore and SQLite→PostgreSQL migration.
- 100% interface parity for the shared operation manifest.
- 100% temporal test correctness.
- 100% citation resolvability and citation-scope correctness.
- **0** cross-scope results across the entire adversarial corpus.
- **0** recorded cloud/network calls in deny mode.
- **0** unreconciled index entries after deletion/rebuild convergence.
- Retrieval baseline, approved before candidate testing:
  - citation precision: **100%**;
  - temporal and correction accuracy: **100%** on deterministic fixtures;
  - stale-fact and unsupported-answer rate: **0%** on deterministic fixtures;
  - Recall@10: **≥95%** and no regression greater than **1 percentage point** from the approved baseline.
- Concurrency/soak: no integrity errors, lost writes, duplicate canonical observations, permanently stuck jobs, or unexplained projection lag; p95 latency must remain within the separately approved capacity budget.
- Two consecutive clean backup/restore rehearsals and two consecutive migration/rollback rehearsals using newly created artifacts.
- Flake policy: critical suites pass **3 consecutive runs** with zero retries; retries cannot convert a failing release gate into PASS.
- Accessibility: zero critical/serious automated violations on critical admin journeys.

### 6. Evidence and verdict contract

Each release report should contain:

- candidate/base SHA and package/image digest;
- exact test commands and unmasked exit codes;
- collection result separately from execution;
- Python/Node/database/browser/model versions;
- fixture and benchmark manifest versions;
- backend-by-interface result matrix;
- failure-injection points exercised;
- raw JUnit, Playwright trace/screenshots, benchmark JSON, scan reports, and semantic digests;
- pre/post test repository status and classification of generated residue;
- defects with severity and reproduction;
- verdict restricted to **PASS**, **BLOCKED**, or **CONDITIONAL**.

`CONDITIONAL` is not release approval: it must identify the exact unmet condition and enforcement owner. Production cutover remains separately authorized.
