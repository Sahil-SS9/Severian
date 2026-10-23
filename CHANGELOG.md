# Changelog

All notable changes to Severian. Dates are release dates (YYYY-MM-DD). Behaviour switches named here are environment
variables; see the Configuration table in the [README](README.md).

## [Unreleased]

### Added
- `SEVERIAN_FILL_BUDGET` (off): search over-fetches a bounded pool (4 per item asked for, capped at 50) and the
  assembled context keeps adding ranked items until its character budget is full. A tight budget is unchanged, because
  it runs out inside the first `limit` items either way.
- `SEVERIAN_COMPACT_CITES` (off): one id per item instead of two. The block cites the source observation ids in the
  bracket and drops the trailing `source:` line, saving about 45 characters per item; every cited id still resolves
  through `severian_get_observation`, `severian_correct` and `severian_forget`.
- `SEVERIAN_SUFFICIENCY` (off): one line after the results when no returned memory holds the specific detail the
  question names, so the reader knows it may not be in memory. Lexical and narrow on purpose - it fires on a missing
  detail slot or on an absent `specific`/`exact` item, and never on a question the returned items answer.

## [0.3.11] - 2026-10-22

### Added
- The MCP server now exposes the full seven-tool surface (`severian_correct`, `severian_forget`,
  `severian_task_progress`, `severian_search_shared` alongside ingest/search/get), so Claude Code, Codex,
  OpenCode and Claude Desktop get feature parity with the Hermes provider. All new tools carry the same
  principal binding, explicit-scope rule and error mapping as the existing three.
- `docs/integrations.md`: MCP recipes per harness, with the verification status of each stated honestly
  (all protocol-verified over live stdio; harness end-to-end runs marked where pending).

## [0.3.10] - 2026-10-22

### Added
- Repo tidied for catalog submission: pitch README with the October competitor round, docs index, CHANGELOG, CONTRIBUTING, SECURITY, issue/PR templates, a runnable example with its own test, and rule-13 disclosures in the plugin directory.

### Changed
- Dependency floors carry upper bounds (pydantic, sqlalchemy, alembic, psycopg, pgvector).
- The GitHub repository is renamed to `Sahil-SS9/Severian` (old links redirect).

## [0.3.9] - 2026-10-21

### Changed
- `SEVERIAN_CONFLICT_NOTES` is **on by default**: when two returned memories disagree, the context ends with a short
  note naming both ("If it matters, mention both."). `SEVERIAN_CONFLICT_NOTES=0` turns it off. Re-test: more BEAM
  answers right with no size limit and at 8,000 characters, never fewer in total.

### Removed
- `SEVERIAN_TIE_ORDER` (same-moment "1st/2nd" marks): correct after 0.3.8's fix, but it changed no answers.

### Unchanged (off)
- `SEVERIAN_REPLY_TRIM` is parked: large gains when the context budget is very tight, losses at 8,000 characters.
- `SEVERIAN_DROP_EMPTY`: no gain in the round 3 re-run.

## [0.3.8] - 2026-10-21 (built for benchmarking, never released to production)

### Added
- Four round 3 switches, all off by default: `SEVERIAN_REPLY_TRIM`, `SEVERIAN_CONFLICT_NOTES`, `SEVERIAN_DROP_EMPTY`,
  `SEVERIAN_TIE_ORDER`. With every switch off, output is byte-identical to 0.3.7.

## [0.3.7] - 2026-10-16

### Changed
- Fact extraction treats a busy or restarting model as retryable: the job goes back to pending (5 to 60 minutes, six
  tries) instead of failing.
- Fact extraction skips machine prompts (cron, kanban dispatch) and profiles listed in `SEVERIAN_FACTS_SKIP_PROFILES`.

## [0.3.6] - 2026-10-11

### Changed
- Much faster exact vector search on large scopes on the `SEVERIAN_OPT_NUMPY=1` path: a candidate cut keeps every
  tie at the boundary, so results are identical. This is the version benchmarked in the October competitor round.

## [0.3.5] - 2026-10-11

### Added
- Task progress notes (`severian_task_progress`: set, get, list, clear), kept as history and never returned by search.
- Embedding sidecar device switch.

## [0.3.4] - 2026-10-10

### Added
- `SEVERIAN_TRUTH_RESOLUTION`: memories a newer fact replaced leave the current view and appear, dated, in history.
- `SEVERIAN_RERANK`: optional cross-encoder re-ranking of the fused candidates.
- Fact-extraction failure logging.

## [0.3.3] - 2026-10-07

### Added
- `SEVERIAN_DATE_STAMPS`: saves the date and weekday with each memory and resolves relative times ("last Tuesday").

### Fixed
- Plugin manifest and provider version now match the package version.

## [0.3.2] - 2026-10-07

### Added
- `SEVERIAN_USER_FACTS`: short facts per conversation turn, extracted in the background.

## [0.1.0] - 2026-08-01

First release: canonical append-only store (SQLite and PostgreSQL), five-dimensional scope, hybrid BM25 + vector
retrieval, correction and rollback, worker jobs, dreaming and curation, CLI, HTTP API, MCP server, export/import and
the operator runbook.
