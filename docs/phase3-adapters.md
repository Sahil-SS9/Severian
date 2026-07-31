# Phase 3 Adapters: MCP & Hermes

This document specifies the contracts and behaviour of the two
adapters added in the second Phase 3 vertical slice: the MCP (Model
Context Protocol) server and the Hermes `MemoryProvider` compatibility
adapter.

Both adapters sit in front of the canonical
`severian.application.service.MemoryService`. Neither adapter writes
the repository, projection indexes, or FTS/vector stores directly.
The architecture rules from `phase3-interfaces.md` continue to apply:
the domain, application, and infrastructure layers remain free of
adapter-specific imports.

```
mcp/   integrations/hermes.py            <-- adapters (Phase 3 slice 2)
   \         /
    application.service.MemoryService
            |
   ingestion + retrieval + projection + derivation + repository
            |
   domain models + application ports
            |
   infrastructure (SQLite, FTS5, vector)
```

## MCP Adapter

Location: `src/severian/mcp/`

### No-dependency design

The MCP server implements JSON-RPC 2.0 over stdio using only the
Python standard library and Pydantic. The optional `mcp` SDK package
is **not** required. The server speaks the wire protocol directly,
which keeps Severian dependency-light and testable in isolation.

### Protocol surface

The server implements the following JSON-RPC methods:

| Method                        | Type          | Behaviour                                    |
|-------------------------------|---------------|----------------------------------------------|
| `initialize`                  | Request       | Returns protocol version + capabilities.     |
| `notifications/initialized`   | Notification  | Marks the session as initialized; no reply.  |
| `ping`                        | Request       | Returns empty result (liveness).             |
| `tools/list`                  | Request       | Returns the three Severian tool definitions.  |
| `tools/call`                  | Request       | Dispatches a named tool with explicit scope.  |

Protocol version: `2025-06-18` (MCP spec version pinned in code).

### Tools

Three tools are advertised via `tools/list`:

| Tool name                     | Purpose                                      |
|-------------------------------|----------------------------------------------|
| `severian_ingest_observation`  | Ingest an observation into the canonical store. |
| `severian_search`              | Hybrid lexical + vector search with optional derive. |
| `severian_get_observation`     | Fetch a single observation by id within a scope. |

### Explicit scope

Every tool argument **must** include the five scope dimensions
explicitly:

- `tenant_id`
- `profile_id`
- `collection_id`
- `agent_id`
- `session_id`

These are validated by a Pydantic model with `extra="forbid"` and a
non-empty validator on each dimension. Missing or blank scope is
rejected with an `isError: true` result **before** the canonical
service is touched. The server never guesses, infers, or defaults
scope from environment variables.

### Tool results

Tool results are MCP `content` blocks of type `text` carrying a safe
JSON payload:

```json
{
  "content": [{"type": "text", "text": "<safe JSON string>"}],
  "isError": false
}
```

Domain/client errors (invalid scope, not found, invalid arguments)
return `isError: true` with a text block containing a safe JSON
error payload. Protocol-level errors (parse error, method not found,
invalid request) use standard JSON-RPC error objects with the
appropriate error codes:

| Code    | Meaning              |
|---------|----------------------|
| -32700  | Parse error          |
| -32600  | Invalid request      |
| -32601  | Method not found     |
| -32602  | Invalid params       |
| -32603  | Internal error       |

### Error behaviour

- **No exception details leak.** Exception tracebacks, internal class
  names, and file paths are never emitted on stdout. The stdio runner
  catches all exceptions and surfaces them as JSON-RPC internal errors
  with the static message `"internal error"`.
- **Malformed JSON** produces a parse error with `id: null`.
- **Non-object requests** produce an invalid request error.
- **Unknown methods** produce a method-not-found error.
- **Pydantic validation errors** are sanitised: only `loc`, `msg`,
  and `type` are returned; the Pydantic error URL (which contains
  `.py`) is stripped to prevent false-positive leak detection.

### Stdio runner

`python -m severian.mcp` provides an executable composition path:

```
python -m severian.mcp \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db
```

The runner composes the on-disk wiring (SQLite repository + FTS5 +
vector index) once, constructs the `SeverianMCPServer`, and runs the
blocking stdio loop. Only protocol JSON is written to stdout;
diagnostics go to stderr. All resources are closed on exit (finally
block). Writes go exclusively through the service; the entry point
never duplicates writes.

### Lifecycle

1. Construct `SeverianMCPServer(service, gateway=None)`.
2. Call `handle_message(raw_json_line)` for each inbound line, or
   `run_stdio()` for a blocking loop.
3. `initialize` → `notifications/initialized` → `tools/list` →
   `tools/call` is the expected client sequence.

## Hermes Compatibility Adapter

Location: `src/severian/integrations/hermes.py`

### No-dependency design

The adapter attempts to import `agent.memory_provider.MemoryProvider`
(the Hermes base class). If the import succeeds, `SeverianMemoryProvider`
subclasses it. If the import fails (Hermes not installed), the class
uses an independent fallback ABC with the same required lifecycle
surface. Severian remains fully independent of Hermes and can be used
without the Hermes package.

### Lifecycle methods

`SeverianMemoryProvider(service, gateway=None)` implements the full
Hermes `MemoryProvider` lifecycle:

| Method             | Behaviour                                                |
|--------------------|----------------------------------------------------------|
| `name` (property)  | Returns `"severian"`.                                    |
| `is_available()`   | Returns `True` when the service health check passes.     |
| `initialize(session_id, **kwargs)` | Establishes five-dimensional scope.        |
| `system_prompt_block()` | Returns a system-prompt description string.          |
| `prefetch(query, *, session_id, **kwargs)` | Searches and returns source-cited bounded context. |
| `sync_turn(user_content, assistant_content, *, session_id, messages)` | Writes a turn observation with safe source metadata. |
| `get_tool_schemas()` | Returns Hermes/OpenAI `parameters` schemas for three tools. |
| `handle_tool_call(tool_name, args, **kwargs)` | Requires explicit matching scope and returns a JSON string. |
| `shutdown()`       | Releases provider-local state only; never closes service resources. |
| `teardown()`       | Compatibility alias that calls `shutdown()`.            |
| `on_session_switch(new_session_id, *, parent_session_id, reset, rewound, **kwargs)` | Re-establishes scope. |
| `on_delegation(task, result, *, child_session_id, **kwargs)` | Records delegation in the established parent scope. |

### Fail-closed scope

The provider **fails closed** when five-dimensional scope cannot be
established. This means:

- `initialize` requires `tenant_id`, `profile_id`, `collection_id`,
  and `agent_id` in kwargs (plus `session_id` as the explicit
  argument). Any missing or blank dimension raises
  `ScopeNotEstablishedError` and clears any previously stored scope.
- All subsequent operations (`prefetch`, `sync_turn`,
  `handle_tool_call`, and `on_delegation`) call `_require_scope()` which raises
  `ScopeNotEstablishedError` if no scope was established.
- Every Hermes tool call must also include all five scope dimensions
  explicitly. They must exactly match the active scope; missing,
  malformed, or mismatched scope is rejected before service access.
- The provider never guesses a tenant, profile, collection, agent,
  or session. There is no default scope, no environment-variable
  fallback, and no caching of scope between sessions.

### Injected service only

All reads and writes go through the injected `MemoryService`. The
provider never accesses the repository, projection coordinator, or
FTS/vector indexes directly. This maintains the single canonical
write path and keeps the adapter auditable.

### No resource ownership

`shutdown()` releases provider-local state (the stored scope and
`session_id`) only. It **never** closes the service's repository or
index resources — those are owned by the caller. `teardown()` remains
as a compatibility alias for Hermes variants that expose that name,
but it is not a substitute for the required `shutdown()` method.
responsible for the service lifecycle.

### Tool calls

`handle_tool_call(tool_name, args, **kwargs)` first requires an active
scope, then validates all five explicit scope dimensions in `args`
against it before routing to the service. The method returns a JSON
string, as required by the Hermes base class. Missing, malformed, or
mismatched scope cannot reach the service.

Three tools are exposed:

| Tool name                | Purpose                              |
|--------------------------|--------------------------------------|
| `severian_search`        | Search within the active scope.      |
| `severian_ingest`        | Ingest an observation.               |
| `severian_get_observation` | Fetch an observation by id.       |

### Source-cited context

`prefetch(query, **kwargs)` searches the memory store and assembles
context using the service's `ContextAssembler`, which produces
source-cited bounded context (each memory block includes
`source: <observation_ids>`). The `max_chars` budget (default 8000)
bounds the output length.

`sync_turn(user_content, assistant_content, *, session_id, messages)`
writes an observation with safe source metadata that always includes
`provider: "severian"` and `provider_version: "0.1.0"`. The optional
assistant response is combined with the user content, and session/message
metadata is recorded without changing the established scope. A `turn`
marker is attached if provided.

`on_delegation(task, result, *, child_session_id, **kwargs)` records the
completed delegation in the established parent scope. The child agent
and child session are provenance metadata only; they never replace the
parent's five-dimensional scope.

## Cross-cutting guarantees

- Both adapters route exclusively through `MemoryService`. No
  direct repository, projection, or index access.
- Scope is explicit and mandatory on every operation. No guessed
  scope, no environment defaults, no cross-scope leakage.
- Cross-scope lookups return a safe "not found" error without
  revealing whether the resource exists in another scope.
- No exception details (tracebacks, class names, file paths) leak
  through either adapter's error responses.
- The optional Hermes dependency is guarded; Severian runs standalone.
- The MCP server uses only stdlib + Pydantic; the optional `mcp` SDK
  is not required.
