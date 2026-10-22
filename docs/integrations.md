# Using Severian from any MCP harness

Severian's MCP server speaks the standard MCP stdio protocol, so any conforming client (Claude Code, Codex,
OpenCode, Claude Desktop, and others) can use it without a platform-specific adapter.

What it exposes (protocol-verified against the server in this repo, 22/10/2026): `tools/list` returns **seven
tools** with the Hermes plugin's full feature set:

`severian_ingest_observation` (save), `severian_search` (hybrid retrieval + `derive:true` for inline draining),
`severian_get_observation` (fetch one), `severian_correct` (revise a wrong fact), `severian_forget` (erase with
audit trail), `severian_task_progress` (cross-session task state), `severian_search_shared` (governed multi-agent
read; your own scope plus explicitly listed agent ids).

Every tool call carries an explicit five-dimensional scope: `tenant_id`, `profile_id`, `collection_id`,
`agent_id`, `session_id`. There is no default scope, and another scope's memories are never returned.

## Standalone store (no Hermes needed)

The MCP server creates and owns its own SQLite store under `~/.severian-mcp/`:

```json
{
  "mcpServers": {
    "severian": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/severian", "python", "-m", "severian.mcp",
               "--database", "~/.severian-mcp/severian.db",
               "--fts", "~/.severian-mcp/severian.fts",
               "--vectors", "~/.severian-mcp/severian.vec"]
    }
  }
}
```

To share one store between the MCP server, the CLI and a Hermes installation instead, point all three at the same
paths (`--database/--fts/--vectors` on the CLI, `SEVERIAN_STORAGE` in Hermes). The server never duplicates writes.

## Claude Code

```bash
claude mcp add severian -- uv run --project /path/to/severian python -m severian.mcp \
  --database ~/.severian-mcp/severian.db --fts ~/.severian-mcp/severian.fts --vectors ~/.severian-mcp/severian.vec
```

Or per-session, without touching any config (what we used for the smoke run):

```bash
claude -p --mcp-config mcp.json --allowedTools "mcp__severian__*"
```

with `mcp.json` as in the standalone block. Verification status: config format is per Claude Code's documented
MCP setup; the server side is protocol-verified. A full Claude Code end-to-end run was not completed on
22/10/2026 (Claude's weekly usage limit) — rerun before relying on it in anger.

## Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.severian]
command = "uv"
args = ["run", "--project", "/path/to/severian", "python", "-m", "severian.mcp",
        "--database", "~/.severian-mcp/severian.db",
        "--fts", "~/.severian-mcp/severian.fts",
        "--vectors", "~/.severian-mcp/severian.vec"]
```

Verification status: the server side is protocol-verified; the Codex config shape follows Codex's documented
`mcp_servers` table and was not end-to-end tested.

## OpenCode

`opencode.json`:

```json
{
  "mcp": {
    "severian": {
      "type": "local",
      "command": ["uv", "run", "--project", "/path/to/severian", "python", "-m", "severian.mcp",
                  "--database", "~/.severian-mcp/severian.db",
                  "--fts", "~/.severian-mcp/severian.fts",
                  "--vectors", "~/.severian-mcp/severian.vec"]
    }
  }
}
```

Verification status: server side protocol-verified; OpenCode config shape per its docs, not end-to-end tested.

## Claude Desktop

Same JSON as the standalone block, in Claude Desktop's developer settings MCP config.

## Verifying any client setup in 10 seconds

```bash
printf '%s\n' \
 '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"0"}}}' \
 '{"jsonrpc":"2.0","method":"notifications/initialized"}' \
 '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
 | uv run --project /path/to/severian python -m severian.mcp --database s.db --fts f.db --vectors v.db | tail -1 \
 | python3 -c "import json,sys; print(sorted(t['name'] for t in json.load(sys.stdin)['result']['tools']))"
```