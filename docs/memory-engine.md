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
- Job completion/lease lifecycle and deletion tombstone propagation remain part of the interface and governance phases.
