# PostgreSQL / pgvector backend

Severian ships a live PostgreSQL backend (`PostgreSQLBackend`) and a
pgvector-backed vector projection (`PGVectorIndex`) that satisfy the
same backend-neutral contracts as the SQLite reference. The parametrized
conformance suite (`tests/contract/test_backends_live.py`,
`tests/contract/test_pgvector_index_live.py`) runs the *same* runtime
behaviour tests against both backends, so the Postgres implementation
cannot silently drift from the reference.

## When to use PostgreSQL

| Concern | SQLite (default) | PostgreSQL (pgvector) |
|---|---|---|
| Deployment | Single process, single file | Network server, service |
| Scale | Personal / single tenant | Multi-tenant / larger corpora |
| Vector search | Python scan over JSON blobs | In-database cosine distance (`<=>`) |
| Backup | File copy (`backup()`) | `pg_dump` / WAL + `backup()` export doc |
| Concurrency | WAL, single-writer | Full MVCC |

Both backends share identical observable semantics:

- Scoped reads filter on the mandatory five dimensions
  (`tenant_id`, `profile_id`, `collection_id`, `agent_id`,
  `session_id`). Cross-scope reads raise `NotFoundError`; the backend
  never reveals that a resource exists in another scope.
- Blank scope dimensions are rejected **before storage access** (the
  backend re-validates because `model_copy` can bypass Pydantic
  construction-time validation).
- Idempotent writes key on the scoped identity; repeated ingestion of
  the same observation/entity/job returns the existing record.
- Observations are immutable; revisions create new rows and set
  `superseded_by_id` on the prior row.
- Exports are byte-compatible (`severian-v1` document, checksummed).

## Running a local server (one-liner)

```bash
docker run -d --name severian-pg \
  -e POSTGRES_USER=severian -e POSTGRES_PASSWORD=severian \
  -e POSTGRES_DB=severian \
  -p 127.0.0.1:5432:5432 \
  pgvector/pgvector:pg16
```
> ⚠️ The credentials above are NON-PRODUCTION placeholder values for local
> development only — replace them before any real deployment.

DSN (SQLAlchemy 2.x + psycopg3; `postgresql://` is auto-normalised to
`postgresql+psycopg://` by both classes):

```
SEVERIAN_PG_DSN=postgresql://severian:severian@127.0.0.1:5432/severian
```

## Using the backend

```python
from severian.infrastructure.postgresql import PostgreSQLBackend
from severian.infrastructure.pgvector_index import PGVectorIndex
from severian.application.derivation import DerivationService
from severian.application.projections import ProjectionCoordinator
from severian.application.worker import DerivationWorker
from severian.infrastructure.fts import FTS5Index

repo = PostgreSQLBackend(dsn=SEVERIAN_PG_DSN)
repo.initialize()  # CREATE EXTENSION vector + tables + indexes

vector = PGVectorIndex(dsn=SEVERIAN_PG_DSN)
vector.initialize()

# Compose the same application stack as the SQLite path:
derivation = DerivationService(repo)
projection = ProjectionCoordinator(repo, FTS5Index("lexical.db"), vector)
worker = DerivationWorker(repo, derivation, projection)
worker.drain(gateway=my_gateway, policy=policy, max_jobs=100)
```

## Conformance

```bash
SEVERIAN_PG_DSN=postgresql://severian:severian@127.0.0.1:54329/severian \
  uv run pytest tests/contract/ -q
```

The live tests skip when `SEVERIAN_PG_DSN` is unset, so the suite stays
green in environments without a Postgres server.

## Schema notes

- `records` is the canonical table (identical shape to SQLite so
  exports are portable): `id`, `record_type`, the five scope columns,
  `content_hash`, `identity_key`, `status`, `payload` (JSONB).
- `memory_vectors` holds the pgvector projection: five scope columns +
  `content` + `vector(256)`. A unique index keys on `memory_id`; a
  scoped index serves `count`/`clear`/`search`.
- `initialize()` is idempotent (`CREATE EXTENSION IF NOT EXISTS`,
  `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`).

## Backup

`PostgreSQLBackend.backup(destination)` writes a portable
`severian-v1` export document (same format as `export_json()`,
restorable via `import_json()`). For physical backups use
`pg_dump`/WAL archiving at the server level; the export doc is the
portable, checksum-verified application-level snapshot.

## Scope isolation (defence in depth)

The five-dimensional scope is enforced at three layers:

1. `Scope` construction rejects blank dimensions (Pydantic validators).
2. Both backends re-validate before storage access
   (`_require_scope`) — guards `model_copy` bypasses.
3. Every query predicate includes all five scope columns; there is no
   unscoped read path on either backend.
