## What changes

## Why

## How it was verified
- [ ] `uv run ruff check src/severian scripts/ tests/`
- [ ] `uv run mypy --strict src/severian tests/benchmarks`
- [ ] `uv run pytest -q` (and the PostgreSQL lane if storage changed)
- [ ] New behaviour is behind a `SEVERIAN_*` switch, off by default, byte-identical when off
- [ ] CHANGELOG.md updated
