# Phase 3 Interfaces

The Phase 3 vertical slice ships three concrete adapters in front of the
canonical application service, plus a versioned benchmark contract.
The architecture rules from `architecture.md` continue to apply: the
domain layer, application layer and infrastructure ports remain free of
FastAPI / Typer / httpx / SDK / MCP imports.

```
sdk/  cli/  api/                  <-- adapters (Phase 3)
   \    |    /
    application.service.MemoryService
            |
   ingestion + retrieval + projection + derivation + repository
            |
   domain models + application ports
            |
   infrastructure (SQLite, FTS5, vector, future pgvector)
```

Adapters MUST go through `MemoryService`. The repository, the
projection coordinator and the indexes are not reachable from
`severian.api`, `severian.cli` or `severian.sdk`; the only entry point
they have is the service and the documented Pydantic request/response
models in `severian.api.schemas`.

## Application service

`severian.application.service.MemoryService` is the only sanctioned
write/read path:

- `ingest_observation(scope, *, content, source)` — canonical
  observation + durable `derive` job. Idempotent on (scope, content).
- `get_observation(scope, observation_id)` — scope-bound lookup. The
  storage layer returns `NotFoundError` for cross-scope lookups; the
  API maps that to a 404 with no information disclosure.
- `search(scope, *, query, limit, derive, gateway, policy)` — runs
  derivation (if `derive=True`) through the supplied
  `DerivationGateway`, rebuilds projections, then runs the hybrid
  retrieval pipeline. Returns `SearchHitView` objects.
- `assemble_context(scope, hits, *, max_chars)` — token-budgeted context
  assembly with source citations, identical to the Phase 2 engine.
- `health()` — readiness probe data; the API exposes it via
  `/readyz`.

`BuildMemoryService(...)` is the composition root. Tests and the
production runtime both use it.

## HTTP API (FastAPI)

Mounted under `src/severian/api/` and constructed by
`create_app(*, service, gateway)` so `TestClient` and `uvicorn` share
the same composition.

Endpoints (versioned under `/api/v1`):

| Method | Path                          | Purpose                                |
|--------|-------------------------------|----------------------------------------|
| GET    | `/healthz`                    | Liveness probe.                        |
| GET    | `/readyz`                     | Readiness probe (service + indexes).   |
| POST   | `/api/v1/observations`        | Ingest an observation.                 |
| GET    | `/api/v1/observations/{id}`   | Fetch by id; scope from query params.  |
| POST   | `/api/v1/search`              | Hybrid retrieval with optional derive. |

Scope invariants on every request:

- `tenant_id`, `profile_id`, `collection_id`, `agent_id`, `session_id`
  are mandatory and non-empty. Empty values are rejected with 422.
- Cross-scope lookups return 404 with a static message; the body
  never reveals whether the id exists in another scope.
- The exception handler returns `{ "detail": "internal error",
  "type": "server_error" }` for unhandled errors; internal exception
  class names and canonical payloads are never echoed back.

The API is a thin layer: validation, mapping, and the safety
filters above. There is no fallback path that bypasses the service.

## Python SDK

`severian.sdk.SeverianClient` is a typed `httpx` wrapper:

- `ingest_observation(*, scope, content, source)`
- `get_observation(*, observation_id, scope)`
- `search(*, scope, query, limit, derive)`

The SDK is strict: passing `scope=None` raises `ValueError`. The
client never invents scope, never caches a default scope between
calls, and propagates server-side `detail` payloads through
`SeverianError(status_code, body)`.

Tests inject a fake `Transport` (see `tests/sdk/test_sdk.py`) to
assert the exact wire payload and to verify that the SDK refuses
to dispatch without an explicit scope.

## CLI

`python -m severian.cli ingest ...` and `python -m severian.cli
search ...` are the two commands. Both accept a `--database` SQLite
path, `--fts` and `--vectors` paths, and the full scope as flags:

```
python -m severian.cli ingest \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant t1 --profile p1 --collection c1 --agent a1 --session s1 \
  --content "Ada founded a company" --source '{"uri":"note://1"}'

python -m severian.cli search \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant t1 --profile p1 --collection c1 --agent a1 --session s1 \
  --query "Ada" --derive
```

The CLI composition root uses `build_default_service` to wire the
infrastructure once; command handlers do not mutate the repository or
indexes directly and close all opened handles on every exit path.

## Cross-cutting guarantees

- All write paths emit canonical events and durable jobs. The API
  and CLI never construct or insert records themselves.
- The default derivation gateway in the CLI is a deterministic
  local hash-based extractor; no network calls are made.
- The SDK and the CLI share the same schemas as the API; a change
  to `severian.api.schemas` is automatically enforced on both.
- `severian.application.service.MemoryService` is the only place
  that calls `DerivationService` and `ProjectionCoordinator`. This
  keeps the canonical write path in a single audited location.

## Out of scope (deliberate seams for the next slice)

- MCP server entry point — left for the next Phase 3 slice.
- Hermes `MemoryProvider` adapter — left for the next Phase 3 slice.
- Bearer / OAuth authentication — left for the next Phase 3 slice.
- Background workers driving the durable outbox — left for the
  next Phase 3 slice. The `derive` job is enqueued but the worker
  loop is not yet wired; the API's `search --derive` path
  short-circuits this for synchronous recall.
