# Severian Benchmark Strategy

Status: ratified 2026-08-02 (branch `audit/brutal-critique-v0.1.0`)
Owner: Severian engineering
Instrument: deterministic provider-comparison harness (in-repo), with
LOCOMO/BEAM taxonomies as design references.

## 1. Benchmark selection — why not LOCOMO/BEAM as the primary instrument

The question "LOCOMO, BEAM, or other?" has a principled answer once you
separate **what is being measured** from **who is being measured**.

### What LOCOMO and BEAM actually measure

- **LOCOMO** (Maharana et al., 2024, arXiv:2404.08183): long-context
  conversational memory. Synthetic human-verified dialogues; probes
  recall, multi-hop, temporal, commonsense, and adversarial questions.
  Scoring: exact-match / token-F1 of an **LLM-generated answer**.
- **BEAM** (Tavakoli et al., 2025, arXiv:2510.27246; ICLR 2026): scale
  buckets 128K–10M tokens, 100 conversations, 2,000 probing questions,
  10 question types, automatic scoring against ground-truth states.

Both benchmarks score an **LLM agent + memory composite**: an LLM
generates the answer from retrieved evidence, and the answer string is
scored. They answer "how good is this agent at using memory?" — not
"how good is this memory platform at storing, retrieving, and
correcting facts?"

Severian is a **storage/retrieval/correction platform**, not an LLM
agent. Running LOCOMO/BEAM against Severian would:

1. conflate retrieval quality with LLM answer generation (an LLM with a
   weak memory can still answer well by paraphrase; a strong memory
   backend gets penalised when the LLM fails to compose);
2. require an external LLM API, breaking the local-first, offline,
   deterministic, reproducible contract this release demands;
3. tell us nothing about the properties we actually ship: scope
   isolation, supersession, stale-fact suppression, crash safety.

### Decision

| Instrument | Role |
|---|---|
| In-repo provider-comparison harness (manifest-driven, deterministic, offline) | **Primary**. Measures the storage/retrieval layer directly: Recall@10, MRR, nDCG@10, citation precision, stale-fact rate, temporal accuracy, cross-scope leakage, unsupported-answer rate, latency p95, cost. |
| LOCOMO query taxonomy | **Design reference** for corpus/query construction (fact, paraphrase, multi_source, correction, temporal, negative, adversarial). Already encoded in the manifest contract. |
| BEAM scale buckets + question types | **Design reference** for the scalability lane (1k / 10k / 100k documents) and for question-type coverage. |
| Full LOCOMO/BEAM agent-loop reproduction | **Future lane** (requires an LLM API + agent loop). Documented in §9; not part of this release gate. |

This keeps the release gate honest: the numbers are produced by running
the runner against the candidate build, not copied from papers.

## 2. Capabilities measured (and their instrument)

| Capability | Primary metric | Instrument |
|---|---|---|
| Fact retrieval | recall_at_10, mrr, ndcg_at_10 | harness, fact/paraphrase queries |
| Multi-source synthesis support | nDCG across weighted evidence | harness, multi_source queries |
| Correction / supersession | stale_fact_rate == 0 | harness, correction queries + stale corpus docs |
| Temporal accuracy | temporal_accuracy | harness, temporal queries |
| Scope isolation (5-D boundary) | cross_scope_leakage == 0 | harness, adversarial + cross-scope queries |
| Abstention on unknowns | unsupported_answer_rate == 0 | harness, negative queries |
| Latency | latency_p95_ms | harness timing |
| Cost | cost_per_query_usd | harness adapter cost metadata |
| Throughput & scale | docs/sec ingest, query p50/p95/p99 at 1k/10k/100k | scale lane (§7) |
| Reliability | crash/lease/reap, atomic abort, idempotent drain | release gates (already green, 254 tests) |

Not measured here (out of scope for this instrument): LLM answer
quality, end-to-end agent task completion (LOCOMO/BEAM), UI, and
operator ergonomics.

## 3. Datasets

- **Primary corpus**: deterministic, seeded, synthetic knowledge-base
  style documents (no personal data, no LLM). Versioned in the
  manifest. Document set is constructed to exercise every query kind
  and to include stale documents, corrections, cross-scope private
  markers, and unanswerable items.
- **Query set**: typed queries (fact, paraphrase, multi_source,
  correction, temporal, negative, adversarial) with expected-evidence
  weights, following the LOCOMO taxonomy. Every answerable query maps
  to ≥1 ground-truth document; every negative query maps to none.
- **Scale corpus**: generated at 1k / 10k / 100k documents from a fixed
  seed so the scale lane is reproducible (BEAM-style bucket growth).

## 4. Tools

| Tool | Role |
|---|---|
| `severian.benchmarks.runner.BenchmarkRunner` | Deterministic run + gate evaluation |
| `severian.benchmarks.manifest` | Versioned manifest validation (schema v1) |
| `severian.benchmarks.cli validate` | CI-friendly manifest check |
| Concrete adapters (`benchmarks/adapters/`) | One per provider; identical protocol |
| Scale lane script | Throughput/latency at corpus sizes |
| `pytest tests/gates` | Reliability gates (crash, restore, deny, supersession) |

## 5. Providers / baselines

Independent lane (run locally, identical conditions):

| Provider | Kind | Deployment |
|---|---|---|
| Severian SQLite | reference backend | local, embedded |
| Severian PostgreSQL | production backend | local PG 16, pgvector |
| ChromaDB 1.5.9 | external, open-source | self-hosted, local client |

Baseline lane (to show the value of the platform, not just the vector
store): a plain lexical (FTS) adapter over the same corpus.

Vendor-reported lane (explicitly labelled, never claimed as
independently reproduced): Mem0, Hindsight, Honcho, SuperMemory,
Mempalace — sourced from their own published materials (§8).

**Security note recorded during setup**: ChromaDB 1.5.9 carries an
unpatched critical pre-auth RCE (CVE-2026-45829, CVSS 10.0,
GHSA-f4j7-r4q5-qw2c). Used here only as a local library in a
throwaway venv, never exposed on a network; still a material
competitive finding.

## 6. Metrics and success criteria

Release-gate thresholds (from `provider-benchmark.md`, unchanged):

| Metric | Threshold |
|---|---|
| recall_at_10 | >= 0.95 |
| citation_precision | >= 1.00 |
| stale_fact_rate | == 0.00 |
| cross_scope_leakage | == 0.00 |
| unsupported_answer_rate | == 0.00 |
| require_external_providers | >= 2 |

Success criteria for the comparison (this document):

- Severian (both backends) meets or exceeds every threshold.
- Severian beats the lexical baseline on every retrieval-quality metric
  (recall/MRR/nDCG) with the same corpus.
- Severian matches or beats ChromaDB on quality metrics; latency and
  resource footprint reported as-is (different architecture, reported
  not gamed).
- Every independently reproduced number comes from a run in this repo
  with raw traces; every vendor number is cited and labelled.

## 7. Methodology

1. Validate the manifest (schema, providers, query contract).
2. Pre-flight canary per adapter: ingest one document, query it, verify
   round-trip before the full run (per benchmark-harness-pattern).
3. Full run: ingest corpus, run typed queries, time every query,
   record per-query traces (raw JSON, SHA of manifest + results).
4. Compute metrics in the runner (no constants substituted for missing
   semantics).
5. Evaluate the gate from the result JSON.
6. Scale lane: for each bucket (1k/10k/100k) measure ingest
   throughput and query latency p50/p95/p99; report monotonicity.
7. Reproducibility: pin manifest SHA, adapter versions, seed, commit
   SHA in every evidence file.

## 8. Competitor comparison policy

- **Independently reproduced** = ran the same manifest through this
  repo's runner in this environment. Only Severian (both backends),
  ChromaDB, and the lexical baseline qualify today.
- **Vendor-reported** = taken from the provider's own publications or
  docs. Recorded with URL + access date; never mixed into the
  independent table. Mem0 (LOCOMO results, ECAI 2025 paper +
  token-efficient algorithm), Hindsight, Honcho, SuperMemory,
  Mempalace, Mnemosyne (Hermes native provider) are in this lane for
  this release.
- Statistical claims: with a deterministic corpus there is no sampling
  variance to report; differences are exact for this dataset. Where
  vendor numbers come from different corpora/conditions, the table
  says so explicitly — no false equivalence.

## 9. Future lanes (documented, not gated)

- **LOCOMO agent-loop reproduction**: requires LLM API access; answers
  the "agent + memory" question. Not run this release.
- **BEAM 10M-token reproduction**: requires the BEAM dataset + LLM;
  not run this release.
- Live hosted adapters (Mem0 API, Hindsight API, Honcho API) once
  credentials and budget are approved.
- Real-data probes (redacted, opt-in) after the synthetic gate passes.

## 10. Deliverables

- `docs/benchmarks/strategy.md` (this file)
- `docs/benchmarks/manifest-v1.json` — deterministic primary manifest
- `docs/benchmarks/evidence/<date>/` — raw per-query traces + gate eval
  + manifest SHA + commit SHA
- `docs/benchmarks/results.md` — results framework + comparison tables
- Scale lane evidence: `docs/benchmarks/evidence/<date>/scale-*.json`
