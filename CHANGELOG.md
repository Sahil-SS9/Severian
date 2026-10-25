# Changelog

All notable changes to Severian. Dates are release dates (YYYY-MM-DD). Behaviour switches named here are environment
variables; see the Configuration table in the [README](README.md).

## [Unreleased]

## [0.3.13] - 2026-10-25

Plugin-catalog review fixes (NousResearch/hermes-agent#142812). No change to memory behaviour.

### Changed
- Severian is published on PyPI. The Hermes plugin folder pins `severian==0.3.13` from the index instead of a local
  wheel path, so a catalog install (which copies only the plugin folder) resolves its dependency. The plugin folder now
  holds only reviewable source. Releases publish from `.github/workflows/release.yml` with trusted publishing and a
  build attestation.
- `SEVERIAN_DSN` is read through Hermes's profile-aware secret scope (`agent.secret_scope.get_secret`) and declared
  under `requires_env` as an optional secret, so one Hermes process serving several profiles never picks up another
  profile's database credentials. Under multiplexing with no active scope the read fails closed. A Hermes without the
  secret scope falls back to the environment, as before.
- The optional Qwen and Granite embedding loaders pass `local_files_only` to the loader instead of setting
  `HF_HUB_OFFLINE` / `TQDM_DISABLE` for the whole Hermes process.

## [0.3.12] - 2026-10-25

Every new switch is off by default; with all of them off, behaviour is byte-identical to 0.3.11 (tested). Measured on
OmniMemEval with the board's models: [docs/benchmarks/omnimemeval-2026-10.md](docs/benchmarks/omnimemeval-2026-10.md).

### Added
- `SEVERIAN_CONTEXT_CHARS` (unset = 8,000, unchanged): the Hermes provider's prefetch context cap, now settable without
  code changes (the loader builds the provider with no arguments). Whole numbers >= 1,000; a bad value logs a warning
  and keeps 8,000 rather than leaving memory unavailable. On stores of long messages this cap is the main
  cost/quality trade-off (OmniMemEval, Hermes default: BEAM 63.9 vs 71.6 and LongMemEval 82.0 vs 90.6 uncapped).
- `SEVERIAN_FILL_BUDGET` (off; measured at the Hermes default: LoCoMo +1.23, 95% range +0.13 to +2.34, no change on
  LongMemEval or BEAM): assembly spends the character window the caller's own items leave empty on the turns
  either side of them, read from the same conversations and appended after those items. It runs no search, so the
  ranking cannot move: the items and their order are unchanged, and with the switch off the block is byte-identical to
  0.3.11. Three seeds, two turns either side, both bounded; nothing is read at all when the items already fill the
  window. The appended memories obey the same closure rule as the `now` view, so a fact that truth resolution closed
  is never put back into a current context.
- `SEVERIAN_COMPACT_CITES` (off): one id per item instead of two. The block cites the source observation ids in the
  bracket and drops the trailing `source:` line, saving about 45 characters per item; every cited id still resolves
  through `severian_get_observation`, `severian_correct` and `severian_forget`.
- `SEVERIAN_LINK_SEEDS` / `SEVERIAN_LINK_WEIGHT` (off, seeds 0, **parked**: lowered multi-hop all-evidence 23.8% -> 16.7% at equal budget
  and slowed search 1.2-1.8x; keep off): name links. A name in one of the best matches
  ("my manager is Kiran") runs one keyword search of its own and pulls in the memories that name the same person
  ("Kiran is allergic to sesame"), scored at `LINK_WEIGHT` times the match so real matches stay first. Targets
  multi-hop questions. Off means the ranking and the number of searches are exactly as before.
- `SEVERIAN_CONTEXT_FORMAT=v2` (off; measured: no change on LoCoMo or LongMemEval, so it stays off): the
  answerer-facing block. It leads with the most directly relevant note, numbers
  each note with its state (current / also true together / earlier, replaced / change) and the date it was said, quotes
  the user's own words for a user fact, and ends with an explicit "if none of these notes is relevant, say you don't
  know". Off keeps the 0.3.11 block byte for byte. **Order with conflict notes** (on by default since 0.3.9): a note's
  state label carries the conflict information on the note it belongs to, so when v2 is on the trailing conflict note
  is not added after the don't-know rule — one voice, nothing repeated.

### Parked
- `SEVERIAN_SUFFICIENCY` (off, **not shippable as designed**): the line fires on a missing *word*, not a missing
  *answer*. An independent probe showed it firing on 20 of 20 answerable questions whose answer is worded without the
  question's slot word ("What is my budget for the trip?" / "I can spend about £500 on the trip"). The code stays in
  the tree behind its switch, off, with its budget guard tested; a future version needs a semantic answerability check
  and a paraphrased negative-control set.

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
