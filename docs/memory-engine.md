# Memory engine

The Phase 2 memory engine is deliberately split into canonical and derived paths:

```text
source input
  -> IngestionService (Unicode/newline normalisation)
  -> SQLite observations + idempotent derive job
  -> DerivationService (scope + cloud policy gate)
  -> SQLite memories with source/model/prompt lineage
  -> ProjectionCoordinator
       -> separate FTS5 projection
       -> separate local vector projection
  -> RetrievalService (reciprocal-rank hybrid search)
  -> ContextAssembler (bounded, source-cited context)
```

## Policy

A gateway is marked cloud-backed with `is_cloud = True`. The derivation service refuses it unless the collection policy contains `{"cloud_allowed": True}`. Local gateways continue to work with the default policy.

## Indexes

`FTS5Index` and `LocalVectorIndex` are derived stores. They are not authorities and can be deleted and rebuilt from `CanonicalRepository.list_memories(scope)`. The current vector implementation uses a deterministic hash embedding as a dependency-free Phase 1 baseline; the `LocalVectorIndex` seam is intended for a stronger local embedding backend later.

`ProjectionCoordinator.status()` exposes canonical-versus-index counts. `index_memory`, `remove_memory` and `rebuild` provide the initial repair controls.

## Retrieval

Results are scope-filtered before ranking. Hybrid ranking uses reciprocal rank fusion across lexical and vector results. Every returned result retains `Memory.source_observation_ids`; `ContextAssembler` emits these source IDs with a character budget.

## Current boundaries

- No network or cloud call is made by the engine itself.
- A derivation gateway is injected by the future model-adapter layer.
- Job leasing, dead-letter recovery and the erasure path are implemented. Purge is an explicit operator action, not an automatic retention step.

## Erase versus purge (M1)

They are different operations, and the docs must not conflate them:

- **Erase** (`erase_memory` / `erase_observation`) is the normal user-facing deletion. It writes a
  synthetic `[reason]` tombstone, supersedes the active head and every derived memory, and
  retracts the relations that depended on the erased observation. The whole cascade is one
  transaction. Search and active reads stop returning the record, but **the text remains in the
  canonical row, in history and in an export**. Erasure also releases the observation's dedupe
  identity so re-stating the same fact later is stored as new evidence.
- **Purge** (`severian purge --observation <id>`, dry run by default, `--confirm` to act) is the
  explicit physical-removal path. It scrubs the observation's whole revision chain, every memory
  derived from it, the relations that lose their last support and the entity names nothing else
  supports. Text becomes the fixed marker `[purged]`, hashes are recomputed over that marker,
  `identity_key` is cleared, and one content-free audit row per target id records what was removed.
  Task notes are deliberately out of scope because they are working state, not canonical memory.
- Purge cannot reach external backups, exported snapshots, PostgreSQL base backups or WAL
  archives. SQLite files are checkpointed and `VACUUM`ed; PostgreSQL physical reclaim is printed
  for the operator, not run. The honest claim is "removed from this store and its index files",
  never "physically purged everywhere". See
  [operator-runbook.md](operator-runbook.md) and
  [threat-model.md](threat-model.md#data-lifecycle).

## Deferred to S2: scheduled maintenance and usage salience

The consolidation, curation, contradiction-scan and retention services exist and are
non-destructive, but **no production path queues `dream` jobs**. A worker can process them if an
operator or a future scheduler enqueues them; the fleet does not do so today. Retention
`apply_archive` is opt-in and keeps history. Curation's salience blend includes a `usage_count`
term, but retrieval usage tracking is off in production, so that term is inert; salience-weighted
ranking in `RetrievalService` is also opt-in with a default weight of zero.

S2 (the Should-wave worker/curation work in the programme plan) is where a real dream-job
producer, scheduling policy and deliberate usage tracking would be designed and tested. Until
then, the README and operator docs describe these as available services, not autonomous
behaviour.

## Correction depth: first order only

Correcting an observation cascades **one hop**. `revise_observation` supersedes the
observation, withdraws every memory derived from it, retracts the relations whose
lineage is that observation, and rebuilds the projection; the derivation guard
stops the superseded head coming back. That is first order, and it is tested.

Nothing links a memory to another memory, so the chain stops there. Second order
is **not** cascaded: a plan, belief or summary built on a corrected memory stays
active and keeps asserting the stale claim, and search returns the corrected fact
and the stale plan side by side. `severian_correct(memory_id=…)` cascades nothing
at all — it replaces that one memory and leaves anything derived from it untouched.

This is documented rather than patched because any change is a write-path change on
the store every profile shares, so it needs its own design and migration. Live
exposure is small today: two `observation.revise` events ever, zero memory-path
corrections, zero active memories with a superseded source.

Tracked as W-2 in `WORKTODO.md`.
