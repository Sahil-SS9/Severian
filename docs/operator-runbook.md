# Severian operator runbook — v0.1.0

Severian is a standalone, proprietary, local-first memory platform. It
stores observations canonically, derives memories, and serves scoped
lexical + vector retrieval. This runbook covers installation, operation,
backup/restore, the PostgreSQL/pgvector backend, and the security
posture for operators.

---

## 1. Installation

### 1.1 From the repository (development / self-host)

```bash
git clone <your-severian-remote> /srv/severian
cd /srv/severian
uv sync                       # installs deps into .venv
uv run severian --help        # verify the console script
```

Requires Python >= 3.11 and `uv` (or `pip install .` after
`pip install -e .`).

### 1.2 Installed binary

The package exposes a console script:

```bash
uv tool install /srv/severian   # or: pipx install /srv/severian
severian --help
```

Commands: `ingest`, `search`, `work`, `export`, `import`.

### 1.3 Layout

The SQLite deployment uses three files (canonical store, FTS index,
vector index). The Postgres deployment uses one database for the
canonical store + one table for vectors (see section 4).

---

## 2. Day-to-day operation

### 2.1 Start the API server

```bash
cd /srv/severian
uv run python scripts/serve_dev.py \
  --host 127.0.0.1 --port 8000 \
  --storage /srv/severian/data/severian.db
```

The dev launcher also starts a derivation worker thread that drains
pending derive jobs every 0.2s, so observations automatically promote
to memories and become searchable.

To expose on a network interface (non-loopback), you MUST provide an
auth file with API keys:

```bash
uv run python scripts/serve_dev.py \
  --host 0.0.0.0 --port 8000 \
  --storage /srv/severian/data/severian.db \
  --auth-file /srv/severian/data/auth.db
```

The launcher refuses to start on a non-loopback host without
`--auth-file` (SEC-01 non-loopback guard).

Health checks:

```bash
curl http://127.0.0.1:8000/healthz   # liveness
curl http://127.0.0.1:8000/readyz     # readiness (repository + projections)
```

### 2.2 Ingest an observation

```bash
severian ingest \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant acme --profile alice --collection main \
  --agent cli --session ses-1 \
  --content "Alice signed the Acme contract" \
  --source '{"uri": "note://contract-1"}'
```

The five scope dimensions (`tenant`, `profile`, `collection`, `agent`,
`session`) are **mandatory** on every command and request. There is no
implicit scope.

### 2.3 Promote / drain derivation jobs

```bash
severian work --database data/severian.db --fts data/fts.db --vectors data/vectors.db
```

Processes all pending derive jobs and rebuilds projections for touched
scopes. In production run this as a loop (the API dev launcher does this
automatically; a systemd unit example is in section 6).

### 2.4 Search

```bash
severian search \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant acme --profile alice --collection main --agent cli --session ses-1 \
  --query "contract"
```

Emit JSON hits with lexical/vector/fused scores and scope.

---

## 3. Backup and restore

### 3.1 Portable snapshot (recommended for app-level backup)

```bash
severian export \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --output /backups/severian-$(date +%F).json
```

The snapshot is a checksummed `severian-v1` JSON document. It is
backend-portable: export from SQLite, import into PostgreSQL and vice
versa.

> **Integrity, not authenticity:** the snapshot checksum verifies that
> the file was not accidentally corrupted or tampered with after
> creation. It does NOT prove who created it or that it came from a
> trusted source. For trusted snapshots (authenticity + tamper-proofing)
> encrypt/MAC the file at rest and keep the key outside the snapshot.

Restore into a fresh store:

```bash
severian import \
  --database data/restored.db --fts data/restored-fts.db --vectors data/restored-vectors.db \
  --input /backups/severian-$(date +%F).json
```

Import validates format, schema version, record shape, checksum, and
scoped-identity conflicts before writing. By default it rebuilds
projections for imported scopes so the restored store is immediately
searchable. Pass `--no-rebuild` to restore canonical records only.

Tampered/corrupt snapshots are rejected (checksum mismatch aborts the
import).

### 3.2 Physical backup (SQLite)

Copy the database file while no writer is active (or use the SQLite
backup API / `sqlite3 .backup`):

```bash
sqlite3 data/severian.db ".backup /backups/severian.db.bak"
```

### 3.3 Physical backup (PostgreSQL)

Use `pg_dump` / WAL archiving at the server level. The app-level
`export` snapshot is the portable, checksum-verified backup; physical
backup covers the full server state.

---

## 4. PostgreSQL / pgvector backend

### 4.1 Run the server

```bash
docker run -d --name severian-pg \
  -e POSTGRES_USER=severian -e POSTGRES_PASSWORD=<redacted> \
  -e POSTGRES_DB=severian \
  -p 127.0.0.1:5432:5432 \
  pgvector/pgvector:pg16
```

### 4.2 DSN

```
SEVERIAN_PG_DSN=postgresql://severian:<redacted>@127.0.0.1:5432/severian
```

`postgresql://` is auto-normalised to `postgresql+psycopg://` (psycopg3).
The `PostgreSQLBackend` and `PGVectorIndex` both call `initialize()`
(`CREATE EXTENSION IF NOT EXISTS vector`, tables, indexes) — idempotent.

### 4.3 Same semantics as SQLite

The parametrized conformance suite (`tests/contract/test_backends_live.py`)
runs identical behaviour tests against both backends. Anything that
works on SQLite works on Postgres: scoped identity idempotency,
immutable observations + revision chains, durable jobs, outbox/audit,
checksummed export/import.

### 4.4 Run conformance (skips when DSN unset)

```bash
SEVERIAN_PG_DSN=postgresql://severian:<redacted>@127.0.0.1:5432/severian \
  uv run pytest tests/contract/ -q
```

---

## 5. Security posture (read before exposing)

Severian is designed **local-first**. The current posture:

- **Loopback binding only by default.** The API is served on 127.0.0.1
  by `serve_dev.py`. Binding to a non-loopback host requires
  `--auth-file`; the launcher refuses to start otherwise (SEC-01 guard).
- **API-key authentication (SEC-01, implemented).** When an auth file is
  configured, the HTTP API is fail-closed: every data endpoint requires a
  valid `Authorization: Bearer <key>` header. Keys are hashed at rest
  (SHA-256), scoped to a principal (tenant/profile), and revocable.
  Generate a key file with the auth store API or a small admin script.
  Without an auth file (loopback/dev mode), the API is open as before.
- **Shared-memory ACL policy (OPS-04).** The access-control boundary is
  the (tenant, profile) pair — the SECURITY-SENSITIVE dimensions. A
  principal's key binds tenant + profile; requests claiming a different
  tenant or profile are rejected 403. Within a tenant/profile,
  collection/agent/session dimensions are SHARED by design: agents and
  sessions of the same tenant/profile can read and write each other's
  memory (collaborative-scope model). There is no per-agent or
  per-session isolation between principals of the same tenant. Before
  public exposure, provision one tenant per untrusted party; do NOT put
  multiple untrusted parties in one tenant and rely on agent/session
  isolation — it does not exist.
- **MCP stdio binding (M3, fail closed).** `python -m severian.mcp` refuses
  to launch without a pinned tenant/profile from `--tenant-id`/`--profile-id`
  or `SEVERIAN_MCP_TENANT_ID`/`SEVERIAN_MCP_PROFILE_ID`, unless the operator
  explicitly passes `--trusted-single-tenant` for a trusted single-user host.
  The binding pins tenant/profile only; collection/agent/session remain chosen
  by the caller. Details and per-harness recipes:
  [integrations.md](integrations.md).
- **No rate limiting.** Acceptable for local-first; add before any
  network exposure.
- **No CORS middleware** (deliberate): browser clients must proxy
  through a server-side route (the dashboard does this).
- **Scope is the security boundary.** The five-dimensional scope is
  mandatory on every request; cross-scope reads return 404 / empty
  results and never leak existence. Blank scope dimensions are rejected
  at the schema and the storage layer.
- **Deletion semantics (OPS-02, implemented at the service layer; reversed for
  purge on owner direction 20/11/2026 — M1).** Observations and memories are
  immutable. Two deletion states exist, and they are different things:
  - **Erasure** (`erase_memory` / `erase_observation`) is tombstone-based: the
    service marks records superseded by a synthetic `[reason]` tombstone so they
    disappear from active reads and search, while history retains the record +
    tombstone for audit. This satisfies regulated "data stops being returned"
    deletion without compromising the immutable audit trail. **Erasure is not
    physical removal:** the text stays in the canonical row, in the export and in
    both histories. Erasing an observation is now ONE transaction (the tombstone,
    every derived memory's tombstone and the relation retractions), so a crash can
    never leave a derived memory active under an erased source, and the erasure
    releases the observation's dedupe identity so re-stating the same fact later
    is stored as new evidence instead of being silently swallowed.
  - **Purge** (`severian purge --observation <id>`) is the real removal, and it is
    an explicit operator action: dry run by default, `--confirm` to carry it out.
    It scrubs the observation's **whole revision chain** (root, corrections and the
    erasure tombstone), every memory derived from it (revisions and tombstones
    included), the relations that lose their last support, and the entity names
    nothing else supports. Every scrubbed text field becomes the fixed marker
    `[purged]`, `content_hash`/`raw_hash` are recomputed over that marker,
    `identity_key` is set to `NULL`, the payload metadata that could carry text is
    emptied (`source`, `attachments`, `cues`, `trigger`), and the record can never
    be an active head again. One append-only audit row per target id
    (`observation.purge`, `memory.purge`, `relation.purge`, `entity.purge`), with ids
    and counts and no content. Pending derivation for the chain is cancelled in the
    same transaction; a **running** derivation job for the chain refuses the purge
    (`ConflictError`) — stop the worker and retry. Task notes are **not** in the
    purge (they are working state, not canonical memory) and there is no flag for
    them.
  - **What a purge cannot do — say this to the user, do not round it up:**
    - copies outside the store are unreachable: `~/.hermes/backups/severian-*`,
      `~/backups/severian-*`, exported snapshot files, PostgreSQL base backups, or
      anything else copied elsewhere. `severian purge` never touches them;
    - on SQLite the CLI checkpoints and `VACUUM`s the canonical, FTS and vector files
      after the purge, which drops the freed pages from the files (their `-wal`/
      `-shm` sidecars are truncated with them);
    - on PostgreSQL the old row versions stay on disk as dead tuples until they are
      vacuumed. An ordinary `VACUUM` does not rewrite pages in place; only
      `VACUUM FULL` rewrites the heap, and WAL archives may still hold old images.
      The CLI **prints** the operator command and does **not** run it:
      `psql "$SEVERIAN_PG_DSN" -c 'VACUUM (FULL, ANALYZE) records;'`.
    - therefore: never claim "physically purged everywhere". The honest claim is
      "removed from this store and its index files".
  - Purging is service + CLI only. There is **no MCP tool** for it: exposing a
    destructive operation to model-chosen callers is a separate decision under the
    MCP-parity rules.

- **Dead-letter inspection/recovery (OPS-07, implemented).** Jobs that
  exhaust their retry budget are marked `dead_letter`. Inspect them with
  `severian dead-letter --database … --fts … --vectors …` (emits JSON
  list) and recover one with `severian dead-letter --requeue <job-id>`
  (resets to pending with a fresh attempt budget; the next `severian
  work` pass retries it). This is the only supported recovery path for
  exhausted jobs — do not edit the database directly.

## 6. systemd unit example (API + worker)

```ini
# /etc/systemd/system/severian.service
[Unit]
Description=Severian memory platform
After=network.target

[Service]
WorkingDirectory=/srv/severian
ExecStart=/srv/severian/.venv/bin/python scripts/serve_dev.py --host 127.0.0.1 --port 8000 --storage /srv/severian/data/severian.db
Restart=on-failure
User=severian
# The dev launcher runs the derivation worker thread in-process.

[Install]
WantedBy=multi-user.target
```

## 7. Verification checklist

After a fresh install:

1. `severian --help` lists all five commands.
2. `python scripts/serve_dev.py ...` → `/healthz` and `/readyz` return
   `"ready": true`.
3. Ingest → `work` → search returns the observation as a hit.
4. `export` → `import` into a fresh store → search returns the same hit
   (restored store is searchable).
5. Tamper with a snapshot → `import` aborts with a checksum error.
6. With `SEVERIAN_PG_DSN` set: `pytest tests/contract/ -q` passes for
   both backends.
