# Severian Architecture

## Product boundary

Severian is a standalone memory platform. It owns canonical records, policies, storage, retrieval, administration, export, deletion and recovery. Hermes, MCP and external providers are adapters.

Supermemory is an external provider and comparison target. It is never Severian's internal authority.

## Data planes

### Phase 1

One local Severian instance contains one isolated data plane:

- canonical SQLite database;
- lexical projection;
- vector projection;
- encrypted configuration and credentials;
- backup/export area;
- local service and web interface.

### Phase 2

Each customer receives an isolated data plane with its own database, indexes, volume, keys, model policy and backups. A shared control plane may manage deployments and billing but cannot read or store customer memory content.

## Layering

```text
Web / CLI / SDK / MCP / Hermes adapter
                  |
            Application API
                  |
    Use cases + policy + authorisation
                  |
        Domain models and ports
                  |
 SQLite/PostgreSQL repositories + index adapters
```

Dependencies point inward. Domain and application modules cannot import FastAPI, Typer, MCP, SQLAlchemy, SQLite, PostgreSQL or provider SDKs.

## Canonical records

- Collection: policy and top-level isolation scope.
- Observation: immutable source content and source metadata.
- Memory: a derived fact, preference, episode, summary, decision or canonical value.
- Derivation: observation-to-memory lineage, model, prompt, confidence and transformation.
- Revision: correction or supersession without destroying prior state.
- Entity: resolved identity within a collection.
- Relation: temporal relationship with validity and provenance.
- Job: durable background work with idempotency.
- Audit event: append-only security and mutation record.

## Indexes

Lexical, vector and graph indexes are projections. Canonical commits emit durable outbox events. Projectors process events idempotently and record the canonical revision indexed. Lag and failure are observable. Complete index deletion and rebuild must be supported.

## Writes

All writes pass through application services and repository transactions. API handlers and workers cannot write tables or indexes directly. SQLite uses WAL, foreign keys, bounded busy timeouts and short transactions. A single controlled writer coordinates concurrent background work.

## Model execution

Model adapters are replaceable. Collection policy selects local-only or optional approved-cloud execution. Cloud-denied collections fail closed before network access. Every derivation stores model identity, provider, configuration fingerprint and prompt/schema version.

## Portability

Stable UUIDs and an explicit versioned export schema are backend independent. Exports include sources, derivations, revisions, entities, relations, policies and audit metadata but exclude rebuildable indexes. Restore validates checksums and schema before activation.
