# Severian

Standalone local-first memory platform. Phase 1 provides backend-neutral domain contracts and canonical SQLite storage. Phase 2 adds ingestion, policy-gated derivation, rebuildable FTS5/vector projections, hybrid retrieval and bounded source-cited context assembly.

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

Canonical evidence is immutable; corrections create revisions. SQLite runs in WAL mode. Export payloads are portable JSON and backups use SQLite's online backup API. See `docs/memory-engine.md` for the canonical-to-index data flow and current boundaries.
