<div align="center">

<img src="assets/severian-hero.png" alt="Severian" width="40%">

# Severian

*Local-first AI memory platform. SQLite or PostgreSQL. Deterministic. Offline-capable. CVE-free core.*

[![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![CI](https://github.com/Sahil-SS9/severian/actions/workflows/ci.yml/badge.svg)](https://github.com/Sahil-SS9/severian/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-326%20passed-brightgreen)](https://github.com/Sahil-SS9/severian)
[![MCP](https://img.shields.io/badge/MCP-Ready-6366f1)](https://modelcontextprotocol.io)
[![Platform](https://img.shields.io/badge/platform-Linux-blueviolet)](https://github.com/Sahil-SS9/severian)

</div>

**Severian** is a standalone, local-first memory platform for AI agents: canonical append-only storage, five-dimensional scope isolation, deterministic hybrid retrieval, correction/supersession, and offline self-curation ("dreaming"). One `uv sync`, one SQLite database — or PostgreSQL with pgvector. No external services required.

---

## Table of Contents

- [Why Severian?](#why-severian)
- [Quick Start](#quick-start)
- [CLI Usage](#cli-usage)
- [Python API](#python-api)
- [MCP Server](#mcp-server)
- [Benchmarks](#benchmarks)
- [Architecture](#architecture)
- [Comparison with Other Memory Providers](#comparison-with-other-memory-providers)
- [Memory Concepts](#memory-concepts)
- [Security & Privacy Model](#security--privacy-model)
- [Configuration](#configuration)
- [Contributing](#contributing)
- [License](#license)

---

## Why Severian?

Most memory layers either never forget (stale facts poison reads) or fabricate on unknowns. Severian is built around five guarantees, all independently measured:

1. **Correction / supersession** — a corrected fact never resurfaces in active search (0.000 stale-fact rate on the quality gate), while the raw evidence stays immutable in history.
2. **Abstention** — unanswerable queries return nothing (0.000 unsupported-answer rate) instead of top-k noise.
3. **Five-dimensional scope isolation** — `tenant / profile / collection / agent / session`, mandatory on every read/write, 0.000 cross-scope leakage.
4. **Determinism** — hash embeddings + BM25 are offline and byte-reproducible across runs; no downloaded-model nondeterminism.
5. **Offline self-regulation** — dreaming (duplicate merge + contradiction flags) and curation (salience scoring, retention policy) run as deterministic worker jobs; nothing is ever silently deleted.

## Quick Start

```bash
git clone https://github.com/Sahil-SS9/severian.git
cd severian
uv sync

# Ingest an observation into a scoped store
uv run severian ingest \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant acme --profile alice --collection main \
  --agent cli --session ses-1 \
  --content "Alice signed the Acme contract" \
  --source '{"uri": "note://contract-1"}'

# Search the scope (--derive runs derivation + projection rebuild first;
# alternatively run `severian work` beforehand for the async worker path)
uv run severian search \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --tenant acme --profile alice --collection main --agent cli --session ses-1 \
  --query "contract" --derive

# Run the derivation worker (production: async derive + projection sync)
uv run severian work \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db
```

Prerequisites: Python 3.12+ and [uv](https://docs.astral.sh/uv/). No network required after install — the default embedding is a local deterministic hash, and all storage is local.

### PostgreSQL backend (optional)

```bash
# Requires PostgreSQL 16 + pgvector
uv run severian ingest \
  --database data/severian.db --fts data/fts.db --vectors data/vectors.db \
  --pg-dsn "postgresql://severian:***@127.0.0.1:54329/severian" \
  --tenant acme --profile alice --collection main --agent cli --session ses-1 \
  --content "Alice signed the Acme contract"
```

The PostgreSQL backend shares the same canonical-store contract as SQLite — idempotent scoped writes, same retrieval semantics, pgvector projections at the embedding's dimension.

## CLI Usage

| Command | Purpose |
|---|---|
| `severian ingest` | Ingest an observation into the canonical store (scope-mandatory) |
| `severian search` | Hybrid search; emits JSON hits with lexical/vector/fused scores + sources |
| `severian work` | Drain derivation jobs; rebuild projections for touched scopes (production loop) |
| `severian dead-letter` | Inspect dead-lettered jobs, or requeue one (`--requeue <id>`) for retry |
| `severian episode` | Consolidate a session's observations into an episode memory (B-2) |
| `severian rollback` | Restore a historical memory revision as the active head (A-2) |
| `severian export` / `severian import` | Portable JSON snapshots with approved-path policy |
| `severian serve` | Launch the HTTP API dev server |

All five scope dimensions (`--tenant --profile --collection --agent --session`) are mandatory on every command. There is no implicit scope.

## Python API

```python
from severian.composition import build_bundle

bundle = build_bundle(
    backend="sqlite",
    database="data/severian.db",
    fts="data/fts.db",
    vectors="data/vectors.db",
)

# Ingest + derive + search through the composed service
bundle.service.ingest_observation(
    scope,
    "Alice signed the Acme contract",
    {"uri": "note://contract-1"},
)
hits = bundle.service.search(
    scope, query="contract", derive=True, gateway=bundle.gateway,
)

# Opt-in Tier 2/3 capabilities (see docs/plans/2026-08-05-ai-memory-concepts.md)
shared = bundle.retrieval.search_shared(scope, ["a2"], "contract")   # multi-agent
related = graph.related_memories(scope, entity_id, depth=2)           # graph hops
bundle.close()
```

The SDK client (`severian.sdk.client`) talks to the HTTP API; the MCP server (`python -m severian.mcp`) exposes the same tools over stdio/SSE.

## MCP Server

Severian ships an MCP server exposing scoped tools (`ingest`, `search`, `episode`-family) with the five-dimensional scope mandatory on every call. Any MCP client (Claude Code, Cursor, Codex, Hermes) can connect:

```json
{ "mcpServers": { "severian": { "command": "uv", "args": ["run", "python", "-m", "severian.mcp"] } } }
```

## Benchmarks

All numbers are independently reproduced with the in-repo runner and manifest — never vendor-mixed. Evidence lives in `docs/benchmarks/evidence/`.

### Quality gate (adversarial-27 manifest, reconciled at SHA 5a5f9f7)

**As of 2026-08-05 (post porter-stemming + query-coverage abstention + derive-guard fixes).** Evidence: `docs/benchmarks/evidence/2026-08-05-head/` + `scripts/run_adversarial_lane.py`.

| Provider | recall@10 | MRR | cit-prec | stale | leak | unsup | gate |
|---|---|---|---|---|---|---|---|
| **Severian SQLite** | 1.000 | 1.000 | 1.000 | 0.000 | 0.000 | 0.000 | **PASS** |
| Baseline (lexical) | 0.933 | 0.718 | 0.562 | 0.173 | 0.000 | 0.062 | FAIL |

The gate now **passes** — citation_precision reached 1.000 after the coverage-abstention + derive-time revision-guard fixes (earlier runs honestly reported 0.938 and FAILed; thresholds were never lowered). The PostgreSQL lane was re-measured at 9749a75 (PASS); it was not re-run at 5a5f9f7 (DSN unavailable in the release session — disclosed in the evidence README).

### LOCOMO-tier conversation recall (30 docs, 16 typed probes)

**Reconciled at SHA 5a5f9f7.** Evidence: `docs/benchmarks/evidence/2026-08-05-head/locomo/`.

| Provider | recall@10 | MRR | cit-prec | stale | temporal | unsup | gate |
|---|---|---|---|---|---|---|---|
| **Severian SQLite** | 0.962 | 0.962 | 0.844 | 0.000 | 1.000 | 0.062 | FAIL (cit-prec < 1.0, unsup > 0) |
| ChromaDB 1.5.9 | 0.962 | 0.833 | 0.531 | 0.144 | 1.000 | 0.188 | FAIL |
| Baseline (lexical) | 0.846 | 0.756 | 0.469 | 0.098 | 1.000 | 0.188 | FAIL |

Honest note: this LOCOMO snapshot is reconciled at current HEAD (2026-08-05) — up from the 2026-08-04 baseline (recall 0.923 → 0.962, MRR 0.885 → 0.962, cit-prec 0.719 → 0.844, unsup 0.188 → 0.062) after the determinism fixes. It still does not pass the release gate on citation precision + unsupported-answer rate; that is a tracked follow-up. Deterministic: two clean runs are byte-identical (C-08 tiebreak fixes).

### Scale (synthetic, hot path = worker-derived)

| Bucket | Severian SQLite | Severian PG | Chroma | Baseline |
|---|---|---|---|---|
| Ingest 1k docs/s | 248 | 170 | 4.2 | 321,026 |
| Query p95 1k (ms) | 69.9 | 83.3 | 247.7 | 1.2 |
| Query p95 100k (ms) | 3,238.6 | 151.6 | n/a (~9h est) | 134.8 |
| Cold derive 100k (s) | 6,458 | 3,147 | n/a | n/a |

Honest limits: cold derive-on-read is super-linear (mitigated by the worker model); Chroma's per-doc ingest is ~4 docs/s; the 100k Chroma lane was not run (would take ~9h). Full caveats in `docs/benchmarks/results.md`.

## Architecture

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
   │  · deterministic local gateway (hash) OR Granite R2 opt-in   │
   │  · skips superseded observations (P0-5)                      │
   │  · EntityExtractionService upserts entities + typed relations│
   └──────────────────────────────────────────────────────────────┘
                 │
                 ├──────────────► ProjectionCoordinator
                 │                 ├──► FTS5 lexical projection (BM25)
                 │                 └──► vector projection (hash default;
                 │                      Granite R2 opt-in, dim auto)
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

Design invariants:
- **Canonical vs derived**: projections (FTS/vector) are rebuildable from canonical; only the canonical store is authoritative (P0-1).
- **Append-only + supersede**: corrections create revisions; old heads stay in history (P0-5); rollback restores them (A-2).
- **Worker lifecycle**: derive/episode/dream jobs are leased, crash-safe, idempotent (P0-4).
- **Scope is the security boundary**: every read/write filters by the 5-D scope; cross-scope reads return empty, never leak existence.
- **Deterministic graph**: EntityExtractionService is rule-based; the same observation always yields the same entities/relations (no LLM, no randomness).

See `docs/memory-engine.md`, `docs/architecture.md`, `docs/operator-runbook.md`.

## Comparison with Other Memory Providers
Severian is a local-first, deterministic memory PLATFORM — not a hosted
service. The honest comparison (feature map, 2026-08-05; measured where
Severian numbers exist, vendor-labelled otherwise). Full matrix:
`docs/benchmarks/comparison-matrix-2026-08-05.md`.

| Capability | **Severian** | Mnemosyne | Honcho | Hindsight | Supermemory | Mem0 | Zep/Graphiti |
|---|---|---|---|---|---|---|---|
| Local-first / offline | **YES (default)** | YES (Hermes native) | no (managed) | YES (MIT) | self-hostable | self-host | self-host |
| Correction / supersession | **YES (0.000 stale, measured)** | partial | partial | partial | resolves contradictions | partial | bi-temporal |
| Immutable audit + rollback | **YES (append-only, A-2)** | ? | no | no | ? | no | partial |
| 5-D scope isolation | **YES (0.000 leakage)** | profile-scoped | multi-tenant | bank-scoped | user-scoped | 4-dim | graph-scoped |
| Abstention (unknown → nothing) | **YES (0.000 unsup)** | partial | no | ? | no | no | no |
| Deterministic embeddings | **YES (hash default)** | n/a | no (LLM) | no (LLM) | no (LLM) | no (LLM) | no (LLM) |
| Temporal reasoning | **YES (1.000)** | ? | partial | YES (belief nets) | YES | partial | **bi-temporal (best-in-class)** |
| Knowledge-update handling | **YES (supersede chain)** | ? | partial | YES | YES | partial | YES |
| Multi-session / episodic | **YES (episode jobs)** | session-based | YES | YES | YES | YES | YES |
| Graph retrieval | **YES (BFS multi-hop)** | ? | partial (peers) | YES (mental models) | YES (memory graph) | partial | **YES (SOTA)** |
| Offline self-curation (dreaming) | **YES (worker jobs)** | YES (dreaming) | partial (deriver) | YES (reflect) | partial | no | no |
| Benchmark transparency | **open harness + evidence** | n/a | no | arXiv paper | MemoryBench open | vendor numbers vary | paper |

Where Severian leads (measured): correction/supersession, abstention,
immutable audit + rollback, scope isolation, determinism, benchmark
transparency. Where it trails (honest): hosted API (roadmap), default
semantic embeddings (Granite opt-in pending A-3), LongMemEval
reproduction (not yet run), dreaming on dense corpora at 10k+ (LSH
pre-filter shipped for diverse corpora — 3.3× consolidate speedup —
dense lanes remain slower). Hindsight leads on structured-belief
conversation memory; Zep/Graphiti on bi-temporal knowledge graphs;
Supermemory on recall speed (sub-300ms) and benchmark marketing.

## Memory Concepts

Severian implements the full AI-memory concept map (see `docs/plans/2026-08-05-ai-memory-concepts.md`):

| Concept | Status | Mechanism |
|---|---|---|
| Scope isolation (5-D) | HAVE | tenant/profile/collection/agent/session, exact-equality, fail-closed |
| Correction / supersession | HAVE | P0-5, 0.000 stale (measured) |
| Temporal queries | HAVE | observation-layer history, 1.000 temporal accuracy |
| Dreaming (consolidation) | HAVE | deterministic Jaccard merge + contradiction flags |
| Self-curation | HAVE | salience scoring + retention policy, never deletes |
| Salience-weighted ranking | HAVE | opt-in decay via `salience_weight` + usage_count |
| Retention policy | HAVE | max_age/min_usage/kinds; evaluate + opt-in archive |
| Graph retrieval | HAVE | entity/relation BFS neighborhood + related_memories |
| Multi-modal ingestion | HAVE | Attachment metadata (image/audio/video/file/transcript) |
| Shared-scope visibility | HAVE | `search_shared(agent_ids)` opt-in multi-agent read |

## Security & Privacy Model

- **Local-first**: no network call is made by the engine itself; the default gateway is deterministic and local. Cloud derivation requires an explicit collection policy (`cloud_allowed: true`).
- **Scope as boundary**: five dimensions mandatory; blank rejected at model + backend (fail-closed). Cross-scope reads return empty/404, never leak existence.
- **Canonical store**: append-only; raw content + SHA-256 hashes preserved; export/import enforces an approved-path policy (symlink rejection, traversal normalisation, overwrite guards).
- **No secrets**: DSNs are env-gated (`SEVERIAN_PG_DSN`, `SEVERIAN_PG_ALLOW_RESET`); repo-wide grep found no credentials.
- **CVE-free core**: unlike the Chroma comparison lane (CVE-2026-45829, critical pre-auth RCE when network-exposed), Severian's core has no such advisory and is designed for network-safe deployment.

## Configuration

Configuration is code-level and per-call, not a single config file:

| Setting | Where | Default |
|---|---|---|
| Backend (sqlite/postgresql) | `build_bundle(backend=...)` | sqlite |
| Embedding provider | `build_bundle(embedding=...)` | HashEmbedding (deterministic); Granite R2 opt-in |
| Cloud derivation gate | Collection `policy: {"cloud_allowed": true}` | false (local only) |
| Derivation mode | `search(derive=...)` or `severian work` | derive-on-read cold; worker hot |
| Abstention gate (recommended) | `search(min_coverage=...)` | None (raw capability) |
| Abstention floor (deprecated) | `search(min_fused_score=...)` | None — unreliable as an absolute floor; use `min_coverage` instead |
| Salience weighting | `search(salience_weight=..., salience_halflife_h=...)` | 0.0 (unchanged ranking) |
| Usage tracking | `search(track_usage=True)` | false (benchmark-safe) |
| Retention policy | `RetentionPolicyService.evaluate/apply_archive(policy)` | report-only |
| PG DSN / reset guard | env `SEVERIAN_PG_DSN`, `SEVERIAN_PG_ALLOW_RESET=1` | unset |

### Abstention gate migration note

**`min_coverage` is the supported abstention gate.** It filters results by
the fraction of query tokens (stemmed, stopword-filtered) present in each
candidate's content — a principled, measurable signal that separates genuine
evidence from lexical decoys. Use `search(min_coverage=0.4)` or
`search_shared(..., min_coverage=0.4)`.

**`min_fused_score` is deprecated and unreliable** as an absolute floor:
genuine weak matches and lexical decoys have overlapping fused-score ranges,
so no single threshold can separate them. The parameter is retained for
backward compatibility but should not be used as the primary abstention
mechanism. It is not removed to avoid breaking existing callers — migrate
to `min_coverage`.

## Contributing

- Gates: `uv run pytest` (326 passed / 36 skipped), `uv run mypy --strict src/severian`, `uv run ruff check src/severian tests scripts`.
- Evidence policy: every benchmark claim must be reproduced with the in-repo runner + manifest; vendor numbers stay labelled and never mixed.
- Determinism policy: no random UUID tiebreaks in ranking; no LLM in the default path; new passes must be byte-reproducible.
- Non-destructive policy: canonical store is append-only; dreaming/curation/retention supersede + mark, never delete.

## License

MIT — see [LICENSE](LICENSE).
