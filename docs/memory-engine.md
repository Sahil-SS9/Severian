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

## Maintenance passes (dream jobs) — S2

The consolidation, curation, contradiction-scan and retention services are non-destructive and are
now reachable two ways, both behind `SEVERIAN_DREAMING` (off by default):

- **One pass, on demand:** `severian dream --mode <m> <scope flags>`. Dry run by default — it
  prints the plan and writes nothing, and calls no model. `--confirm` carries the pass out.
  Modes: `consolidate`, `curate`, `retention`, `retention-archive`, `contradictions`.
- **The worker tick:** `severian work --dream-tenant T --dream-profile P` queues one `dream` job
  per (active scope, tick mode) and then runs them. The tick queues `consolidate`, `curate` and
  `contradictions` only: retention and retention-archive stay explicit operator actions. It cannot
  queue a mode twice — `enqueue_job` is idempotent on (kind, payload) — so a second tick queues
  nothing. No cron is created or enabled by this release.

**Code decides, a model may only write.** Every candidate pair is classified deterministically as a
*duplicate* (supersede the older head), a *contradiction* (value-bearing difference: a number, a
negation or a name — reported, never resolved) or a *restatement* (different words, same fact — no
write). A model is optional and its only job is the text of a merge summary: batches of eight, one
retry per the busy-model rules, a failed batch falls back to the deterministic summary text and is
counted, and the pass stops calling the model once ~10% of its batches have failed. The writer must
be loopback unless `SEVERIAN_DREAM_ALLOW_REMOTE=1`, so the default path cannot spend money.

**Maintenance markers are not memories.** `retention_archive`, `curation_review` and the
`consolidated` merge summary are records about a pass. They are never returned by `search` and never
rendered by `assemble_context`, on either context format and not through the fill-budget read
either (`severian.application.markers`). This is a fix, not a switch: they were ordinary searchable
memories before, at confidence 1.0.

**Convergence and safety.** Markers are excluded from the passes' own inputs, so a second pass
converges instead of writing another marker. Supersedes go through M2's guarded path: a lost race
(`ConflictError`) is skipped and logged as `dreaming.skip`, never a failed pass. Every dream write
carries an audit row — `dreaming.merge`, `dreaming.consolidate`, `curation.report`,
`retention.archive`, and `retention.archive_plan` written before anything is archived.

**Usage is not a salience input.** Curation's blend is confidence and recency only. Retrieval usage
tracking is off in production (nothing passes `search(track_usage=True)`), so the removed
`usage_count` term always contributed exactly 0.0; the score is unchanged on every real store and a
test pins it from both sides.

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
