# Severian Release-Gate Objective Review — Stored Evidence

> This is the durable copy of the objective review output (2026-08-05).
> The review was produced by an independent adversarial review of the
> Severian repo at base HEAD `8c8a8009e6218e5f5087cbcd5f9a87b96cc07667`.
> It covers: authentication, job recovery, import/backup integrity,
> correction atomicity, composition-root cleanup, evidence reconciliation,
> and the low-risk architecture repair candidate bundle (AR-01..AR-07).
>
> Release status at time of writing: **BLOCKED for networked/multi-user
> deployment; CONDITIONAL for trusted single-user loopback use.**

---

## 1. Simple high-level summary

Severian's core idea is good. The canonical memory model, supersession,
scope filtering, deterministic retrieval and non-destructive maintenance
are worth keeping.

The main problem is not that the project needs a rewrite. It needs its
trust boundary and operational lifecycle finished.

The highest risks are:

1. The default HTTP, MCP, Hermes and SDK paths do not authenticate callers.
   Scope fields are validated, but they are still client-selected.
2. Failed jobs can become permanently stuck, and the outbox/reaper recovery
   machinery is not fully wired into runtime operation.
3. Imports, backups and restores can bypass parts of the provenance,
   authorisation and path-safety model.
4. Runtime entrypoints diverge. CLI, MCP and API do not necessarily construct
   or process memory in the same way.
5. Several benchmark and documentation claims are broader than the committed,
   current-HEAD evidence supports.
6. The low-risk architecture repair candidate fixes graph matching, shared
   abstention, contradiction detection and LSH handling, but it is isolated
   and still needs final verification before it can be accepted.

Recommended order:

1. Authentication and principal-bound scope.
2. Job recovery and durable projection/replay.
3. Import, backup, restore and provenance integrity.
4. Correction/derivation atomicity.
5. Composition-root and port cleanup.
6. Evidence and documentation reconciliation.
7. Only then further benchmark and product expansion.

---

## 2. Converged agent checklist (instructions)

- Work from a clean isolated branch. Do not modify the canonical checkout
  without explicit approval.
- For every item:
  - inspect the current code before editing;
  - write a regression test first;
  - capture RED;
  - implement the smallest correct fix;
  - capture GREEN;
  - run the relevant broader suite;
  - record changed paths, commit SHA, command and exit code;
  - do not mark an item complete from a passing test alone if the real
    runtime path is not exercised;
  - do not run 10k/100k benchmarks unless explicitly authorised;
  - keep historical evidence separate from fresh evidence;
  - do not silently widen scope.

Reference baseline:

- Repository: /home/kensei/repos/Severian
- Branch: feat/ai-memory-concepts
- Base HEAD: 8c8a8009e6218e5f5087cbcd5f9a87b96cc07667

Candidate repair worktree:

- /home/kensei/worktrees/severian-architecture-repairs-2026-08-05
- Current candidate includes e668f80, a9ee8fa and c027c65
- Candidate-only changes are not complete until independently re-verified
  and explicitly accepted.

---

## 3. Full item register

### Phase 0 — Candidate repair bundle (verify / merge when approved)

- AR-01 Graph token-boundary matching
- AR-02 Shared retrieval abstention
- AR-03 Single contradiction detector
- AR-04 LSH duplicate grouping and edge cases
- AR-05 Truthful worker contract
- AR-06 Retention provenance
- AR-07 Documentation and portable script corrections

### Phase 1 — Critical security blockers

- SEC-01 Implement authentication and principal-bound scope [P0]
- SEC-02 Protect administrative export/import/backup operations [P0]
- SEC-03 Enforce sensitive-file permissions [P0/P1]

### Phase 2 — High-priority data integrity and recovery

- DATA-01 Close the import provenance gap [P1]
- DATA-02 Make restore staged and atomic [P1]
- JOB-01 Repair failed-job retry and dead-letter semantics [P1]
- DATA-03 Make correction end-to-end durable [P1]
- DATA-04 Make graph extraction and memory publication atomic [P1]
- DATA-05 Enforce collection ownership [P1]
- DATA-06 Fix batch memory lookup scope [P1/P2]
- DATA-07 Fix usage-count lost update [P1]

### Phase 3 — Operational lifecycle

- OPS-01 Wire the outbox projector [P1]
- OPS-02 Implement deletion/erasure lifecycle [P1 for regulated/multi-user]
- OPS-03 Remove or bound synchronous derive=True [P1/P2]
- OPS-04 Add shared-memory ACLs [P1 before public exposure]
- OPS-05 Make curation/retention/consolidation idempotent [P1/P2]
- OPS-06 Make consolidation failure-atomic [P2]
- OPS-07 Add dead-letter inspection and recovery [P2]

### Phase 4 — Architecture and simplification

- ARCH-01 Collapse composition roots [P1/P2]
- ARCH-02 Establish one authoritative projection contract [P2]
- ARCH-03 Stop application code reaching into infrastructure [P2]
- ARCH-04 Remove _MemoryProxy reconstruction [P2]
- ARCH-05 Correct CLI extraction divergence [P1]

### Phase 5 — Evidence, CI and documentation

- EVID-01 Fix CI dependency bootstrap [P1]
- EVID-02 Produce current-HEAD full benchmark packages [P1]
- EVID-03 Reconcile adversarial-27 evidence [P1]
- EVID-04 Reconcile LOCOMO evidence [P1]
- EVID-05 Scope determinism claims correctly [P2]
- EVID-06 Add provenance to dreaming evidence [P2]
- EVID-07 Correct scale-runner documentation [P3]
- EVID-08 Add dependency/security scanning [P2]
- EVID-09 Correct schema migration verification [P2]
- EVID-10 Resolve warning and test-count claims [P3]

### Final release gate

Not release-ready until ALL of: principal auth, non-loopback protection,
import provenance, staged atomic restore, job retry/dead-letter, lease
reaping + outbox consumption, durable correction, collection ownership,
backup/export policy, deletion semantics or explicit restriction,
composition-root convergence, current benchmark evidence, clean CI,
explicit PG/integration skips, independent final rerun, no worktree
residue.
