<div align="center">

<img src="assets/severian-hero.png" alt="Severian" width="40%">

# Severian

**Long-term memory for AI agents that runs on your own machine, remembers where every fact came from, and keeps
every user's memories apart.**

[![Python](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/Sahil-SS9/Severian/actions/workflows/ci.yml/badge.svg)](https://github.com/Sahil-SS9/Severian/actions/workflows/ci.yml)
[![MCP](https://img.shields.io/badge/MCP-ready-6366f1)](#use-it-from)

</div>

Severian gives an agent a memory it can trust. It saves what was said exactly as it was said, derives short searchable
memories from it, and hands the answering model a compact, cited context block for each question. It runs on SQLite
out of the box (PostgreSQL + pgvector when you need it), works fully offline, and plugs into Hermes Agent, any MCP
client, a CLI, an HTTP API or plain Python.

## Results

Severian on [OmniMemEval](https://github.com/MemTensor/OmniMemEval), the public harness behind its long-term-memory
leaderboard, using the board's own answer model (`gpt-4.1-mini`), judge (`gpt-4o-mini`), prompts and metrics
(October 2026; [full results](docs/benchmarks/omnimemeval-2026-10.md)):

![Severian vs LCM-X on OmniMemEval](docs/benchmarks/img/h2h.svg)

| System | LoCoMo (1,540 q) | LongMemEval (500 q) | BEAM 100K (400 q) |
|---|---:|---:|---:|
| **Severian 0.3.11** | **87.34** | **90.60** | 71.62 |
| LCM-X v0.28.1 | 79.48 | 86.60 | **71.91** |
| Severian minus LCM-X (95% range) | **+7.86** (+6.04 to +9.74) | **+4.00** (+1.20 to +6.60) | -0.29 (-3.16 to +2.55), a tie |

- **Ahead on LoCoMo and LongMemEval, level on BEAM**, paired on the same questions. A stricter second marker (Claude
  Opus, official LongMemEval templates) agrees on all three.
- **The setup is fair to the other side:** run through the same pipeline, LCM-X scores within 0.65 points of its own
  published board numbers.
- **Where Severian sits on the board.** These are our own runs, not an official entry, and we compared head-to-head
  with LCM-X only, so treat the placement as context:

![OmniMemEval leaderboard context](docs/benchmarks/img/board.svg)

- **Trade-offs, stated plainly.** Severian hands the answering model more text than LCM-X (3.4x on LoCoMo, ~1.2-1.3x
  elsewhere) and makes model calls when it saves memories. Cut to the same amount of text, it stays clearly ahead on
  LoCoMo and ties on BEAM. Hermes ships Severian with a cheap 8,000-character context cap; that costs little on LoCoMo
  but about 8 points on BEAM and LongMemEval ([settings](docs/benchmarks/omnimemeval-2026-10.md#severians-own-settings)).
- **We found a marking flaw in BEAM** that rewards made-up answers to "the chat never said" questions. It inflates
  every system's BEAM score, ours included ([details](docs/benchmarks/omnimemeval-2026-10.md#a-marking-flaw-in-beam)).
- **Zero leaks, zero stale facts.** On Severian's own adversarial quality gate, a corrected fact never comes back in
  search (0.000 stale-fact rate) and no query ever returns another scope's memory (0.000 leakage).

Every number, the adapter, the plan and its changes, and the limitations are in
[docs/benchmarks/omnimemeval-2026-10.md](docs/benchmarks/omnimemeval-2026-10.md). Our earlier internal round against
Mem0, Mnemosyne, Hindsight and Honcho (different harness and marker) is in
[docs/benchmarks/competitor-round-2026-10.md](docs/benchmarks/competitor-round-2026-10.md).

## Why Severian

- **Nothing is lost, nothing is made up.** Raw observations are append-only and hashed. Every memory and every search
  hit cites the observation it came from, and corrections create a new revision instead of overwriting history, so
  any earlier version can be restored.
- **Scope is the security boundary.** Every read and write names five levels: tenant, profile, collection, agent and
  session. There is no default scope, and another scope's memories are never returned or even hinted at. Agents can
  opt in to reading an explicit list of teammates' scopes.
- **Built for real conversations.** Short facts per turn, real dates for relative times, newer facts that close older
  ones, and a note when two memories disagree, so the agent answers with the current truth.
- **Hybrid search that holds up.** BM25 and vector search fused with reciprocal rank fusion, an optional cross-encoder
  re-ranker, an entity graph for multi-hop lookups, and a context assembler that keeps the most useful items inside a
  character budget.
- **Local-first.** No network call from the engine; cloud derivation needs an explicit per-collection opt-in. Qwen3
  0.6B embeddings run locally or in a shared sidecar; a deterministic hash embedding needs nothing at all.
- **Looks after itself.** Background "dreaming" merges near-duplicates and flags contradictions, curation scores
  salience, and retention policies archive instead of deleting.
- **Operable.** Crash-safe leased worker jobs, dead-letter recovery, export/import, side-by-side vector rebuilds, an
  integrity audit, per-profile fact rollback and an optional Rust acceleration module.
- **Harness-agnostic.** The same engine serves any agent framework through the CLI, the Python SDK, the HTTP API or
  MCP, and runs unchanged on SQLite or PostgreSQL; the Hermes plugin is one adapter on top.

## How it works

```text
                        ┌──────────────────────────────────────────┐
                        │                SOURCE INPUT              │
                        │  text observation · attachments (multi-  │
                        │  modal metadata: image/audio/video/file) │
                        └──────────────────┬───────────────────────┘
                                           │
                                           ▼
                        ┌──────────────────────────────────────────┐
                        │          IngestionService                │
                        │  normalise · hash raw · source identity  │
                        └──────────────────┬───────────────────────┘
                                           │
                 ┌─────────────────────────┼─────────────────────────┐
                 ▼                         ▼                         ▼
   ┌───────────────────────┐   ┌───────────────────────┐   ┌───────────────────────┐
   │   CANONICAL STORE     │   │   derive job queue    │   │  Entity/Relation       │
   │  (SQLite | PostgreSQL)│──▶│  (leased, crash-safe, │──▶│  Graph (auto-populated │
   │  observations         │   │   idempotent)         │   │  by EntityExtraction)  │
   │  memories             │   └───────────────────────┘   └───────────────────────┘
   │  entities/relations   │
   │  jobs · audit events  │   append-only · scoped · hashed · supersede-aware
   └───────────────────────┘
                 │
                 ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  DerivationService (worker drains derive/episode/dream jobs) │
   │  · policy-gated (cloud_allowed false by default)             │
   │  · deterministic hash OR Qwen 0.6B semantic opt-in          │
   │  · skips superseded observations (P0-5)                      │
   │  · EntityExtractionService upserts entities + typed relations│
   └──────────────────────────────────────────────────────────────┘
                 │
                 ├──────────────► ProjectionCoordinator
                 │                 ├──► FTS5 lexical projection (BM25)
                 │                 └──► vector projection (hash default;
                 │                      Qwen 0.6B semantic opt-in, 384-dim)
                 │
                 ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  RetrievalService (hybrid RRF fusion)                        │
   │  · coverage abstention gate    · salience-weighted ranking   │
   │  · shared-scope read (search_shared, opt-in multi-agent)     │
   │  · temporal query orientation (active head first for present)│
   └───────────────┬──────────────────────────────────────────────┘
                   │
                   ├──────────────► ContextAssembler (bounded, source-cited)
                   │
                   ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  GraphRetrievalService                                       │
   │  · BFS neighborhood over Entity/Relation (both directions)   │
   │  · temporal validity filtering (expired relations excluded)  │
   │  · related_memories by min graph distance                    │
   └──────────────────────────────────────────────────────────────┘

   OFFLINE SELF-REGULATION (dream worker jobs, non-destructive):
   ┌──────────────────────────────────────────────────────────────┐
   │  ConsolidationService  · Jaccard near-dup merge (>= 0.8)     │
   │  CurationService       · salience scoring (recency+usage+    │
   │                          confidence), review markers only    │
   │  RetentionPolicyService· max_age/min_usage/kinds policy,     │
   │                          evaluate + opt-in archive           │
   │  ContradictionScanService · report-only, never auto-resolves │
   └──────────────────────────────────────────────────────────────┘
```


- The **canonical store** is the only source of truth. Search indexes are projections that can be rebuilt from it at
  any time.
- **Worker jobs** derive memories and facts in the background, so saving stays fast. A failed job retries and then
  dead-letters; it never silently disappears.
- **Retrieval** runs inside one exact scope, fuses lexical and semantic results, and drops memories a newer fact has
  closed from the current view (they stay in history).

More: [architecture](docs/architecture.md), [memory engine](docs/memory-engine.md), [threat model](docs/threat-model.md).

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/Sahil-SS9/Severian.git
cd severian
uv sync

# Save an observation in a scope
uv run severian ingest \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant acme --profile alice --collection main --agent cli --session ses-1 \
  --content "Alice signed the Acme contract on Friday" \
  --source '{"uri": "note://contract-1"}'

# Search it (--derive processes pending jobs first; production runs `severian work`)
uv run severian search \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant acme --profile alice --collection main --agent cli --session ses-1 \
  --query "contract" --derive
```

Or run the Python example, which saves memories for two users, searches one and builds a cited context:

```bash
uv run python examples/quickstart.py
```

## Use it from

| Surface | How |
|---|---|
| **Hermes Agent** | Drop-in memory provider: build with `scripts/build_hermes_plugin.py`, install as `$HERMES_HOME/plugins/severian/`, set `memory.provider: severian`. Tools: search, ingest, correct, forget, shared search, task progress notes. |
| **MCP** | full seven-tool surface over stdio for Claude Code, Codex, OpenCode and Claude Desktop: [docs/integrations.md](docs/integrations.md). Tools: ingest, search, get, correct, forget, task progress, shared search. |
| **Python** | `severian.composition.build_bundle(...)`; see [examples/quickstart.py](examples/quickstart.py) |
| **HTTP** | `uv run severian serve` (dev server); client in `severian.sdk.client` |
| **CLI** | `severian ingest / search / work / export / import / audit / rebuild-vectors / ...` (`--help` lists all) |

## Configuration

Behaviour switches are environment variables, so the same code serves benchmarks, tests and production.

| Setting | Default | What it does |
|---|---|---|
| `SEVERIAN_EMBEDDING` | `hash` | `qwen` (local Qwen3-Embedding-0.6B) or `remote-qwen` (shared sidecar at `SEVERIAN_EMBED_SOCKET`) |
| `SEVERIAN_USER_FACTS` | off (**recommended on**) | Short facts per conversation turn, extracted by a local model in the background |
| `SEVERIAN_DATE_STAMPS` | off (**recommended on**) | Writes real dates next to relative times when saving |
| `SEVERIAN_TRUTH_RESOLUTION` | off (**recommended on**) | Memories a newer fact replaced leave the current view and stay in history |
| `SEVERIAN_RERANK` | off (**recommended**: `cross-encoder/ms-marco-MiniLM-L6-v2`) | Re-ranks the fused candidates |
| `SEVERIAN_OPT_NUMPY` | off (**recommended on**) | Fast exact vector scoring on large scopes |
| `SEVERIAN_CONFLICT_NOTES` | **on** | Adds a short note when two returned memories disagree (`0` turns it off) |
| `SEVERIAN_FILL_BUDGET` | off | Search over-fetches a bounded pool and the context keeps adding ranked items until its character budget is full, instead of stopping at `limit` |
| `SEVERIAN_COMPACT_CITES` | off | Cites the source observation ids in one bracket and drops the trailing `source:` line: about 45 characters less per item, every citation still resolvable through `severian_get_observation` |
| `SEVERIAN_SUFFICIENCY` | off | Adds one line after the results when no returned memory holds the specific detail the question names |
| `SEVERIAN_STORAGE` / `SEVERIAN_DSN` | Hermes store / unset | SQLite directory, or a PostgreSQL DSN |

The recommended settings are the ones benchmarked above. PostgreSQL setup:
[docs/postgresql-backend.md](docs/postgresql-backend.md). Day-to-day running: [docs/operator-runbook.md](docs/operator-runbook.md).

## Roadmap

Two moves carry the next round: per-person fact sheets to close the multi-hop gap, and a better rule for knowing when
to answer "I don't know". Then a wider field (MemMachine, Graphiti/Zep, Letta, Cognee, LangMem, Memobase), a
cloud-model check of the winning setup, and stricter marks: every in-house run re-marked by Opus, plus cost per
memory, a 10x-size stress test, and a confident-wrong vs "I don't know" measure.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop and quality gates, and [SECURITY.md](SECURITY.md) to
report a vulnerability. Release history: [CHANGELOG.md](CHANGELOG.md). All documentation: [docs/](docs/README.md).

## License

MIT. See [LICENSE](LICENSE).
