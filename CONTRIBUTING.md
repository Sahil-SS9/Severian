# Contributing to Severian

Thanks for helping. Severian is a memory system, so the bar is correctness first: a change that makes one benchmark
column better and another worse is not finished.

## Development loop

```bash
uv sync --group dev
uv run ruff check src/severian scripts/ tests/
uv run mypy --strict src/severian tests/benchmarks
uv run pytest -q
```

- Unset inherited `SEVERIAN_*` variables before running the suite (`tests/conftest.py` drops them, but a shell that
  exports live settings can still confuse ad-hoc scripts).
- PostgreSQL lane, the same as CI: start `pgvector/pgvector:pg16` on `127.0.0.1:54329` (user, password and database
  `severian`), then run the suite with `SEVERIAN_PG_DSN=postgresql://severian:severian@127.0.0.1:54329/severian` and
  `SEVERIAN_PG_ALLOW_RESET=1`.
- Optional Rust module: `cd native && cargo fmt --check && cargo clippy --all-targets -- -D warnings`, then build it
  with `maturin build --release` and run the native test files listed in `.github/workflows/ci.yml`.

## Rules every change follows

- **Tests first.** A behaviour change comes with a test that fails before it and passes after it.
- **Scope is never optional.** Every read and write names all five scope levels; no code path may widen a scope.
- **Append-only.** The canonical store is never rewritten or deleted from; corrections, dreaming, curation and
  retention create revisions or markers.
- **New behaviour ships behind a switch** (`SEVERIAN_*`, off by default) until a benchmark shows it helps. With the
  switch off, output must be byte-identical to the previous release.
- **Benchmark claims are reproduced, not quoted.** Every number in the README or `docs/benchmarks/` cites the run it
  came from; vendor numbers stay labelled and are never mixed with ours.
- **Determinism.** No random tie-breaks in ranking; the same input gives the same output.

## Releasing

A version lives in five places (`pyproject.toml` + `uv.lock`, `PROVIDER_VERSION` in
`src/severian/integrations/hermes.py`, and `plugins/hermes/severian/plugin.yaml` + `pyproject.toml`);
`tests/integrations/test_hermes_plugin_packaging.py` fails if one is missed. Add a [CHANGELOG](CHANGELOG.md) section,
build the plugin with `scripts/build_hermes_plugin.py`, then tag `vX.Y.Z`.

## Pull requests

Keep them small and focused, describe the behaviour change and how you verified it, and fill in the PR template.
