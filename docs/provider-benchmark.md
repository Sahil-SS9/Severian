# Provider Benchmark

## Requirement (Phase 3 release gate)

Before any Phase 3 release, Severian MUST be compared against at least
**two external memory providers** through the same query set:

- one **hosted** external provider;
- one **self-hosted / open-source** external provider where feasible.

For every provider the benchmark MUST report:

- `recall_at_10`
- `mrr`
- `ndcg_at_10`
- `citation_precision`
- `stale_fact_rate`
- `temporal_accuracy`
- `cross_scope_leakage`
- `unsupported_answer_rate`
- `latency_p95_ms`
- `cost_per_query_usd`

The release gate is encoded in the manifest and enforced by the
runner. The default gate is:

| Metric                  | Threshold                       |
|-------------------------|---------------------------------|
| `recall_at_10`          | `>= 0.95`                       |
| `citation_precision`    | `>= 1.00`                       |
| `stale_fact_rate`       | `== 0.00`                       |
| `cross_scope_leakage`   | `== 0.00`                       |
| `unsupported_answer_rate`| `== 0.00`                       |
| `require_external_providers` | `>= 2`                    |

A release cannot be approved if any gate metric is below threshold or
if fewer than two external providers participated. The gate
configuration is part of the manifest so release engineering can
adjust it deliberately, with a written justification, and so the
runner enforces it deterministically.

**Numbers are not copied into source.** Every benchmark figure in
release evidence MUST be produced by running
`severian.benchmarks.runner.BenchmarkRunner` against the candidate
build with a pinned manifest. The runner has no network surface of
its own: external adapters plug in via the `ProviderAdapter`
protocol and may run live calls; the manifest records the model
identity, prompt version, configuration fingerprint and provider
deployment so the run is reproducible.

## Manifest contract

`docs/benchmarks/phase3-smoke.json` is the seed manifest. A valid
manifest is a JSON object with `schema_version: 1` containing:

- `corpus` — versioned, seeded, non-empty list of documents and
  their scope labels.
- `queries` — non-empty list of typed queries (`fact`,
  `paraphrase`, `multi_source`, `correction`, `temporal`, `negative`,
  `adversarial`) with `expected_evidence` weights.
- `providers` — at least one `severian` provider and at least one
  `external` provider per `release_gate.require_external_providers`,
  with at least one `hosted` and one `self-hosted` deployment.
- `metrics` — the required metric set above. The manifest loader
  rejects missing metrics.
- `release_gate` — explicit thresholds for the gate metrics.

The loader is `severian.benchmarks.manifest.load_manifest`; it raises
`ManifestValidationError` for any structural problem so CI can fail
the gate without a custom parser.

## Runner

`severian.benchmarks.runner.BenchmarkRunner.run()` returns a
`BenchmarkResult` with one `ProviderResult` per registered provider and
an explicit `gate_passed`/`gate_failures` result. Every adapter must
return `ProviderQueryResponse(items, cost_usd)`; missing cost or
malformed response metadata fails the run rather than producing a
plausible-looking number.

For each query the runner:

1. calls `adapter.ingest(document_id, content, scope)` for every corpus
   document (once per provider);
2. calls `adapter.query(query, limit=10, scope=<query scope>)`, times it,
   and records p95 latency plus explicit cost;
3. computes `recall_at_10`, `mrr`, `ndcg_at_10`, citation precision
   against the expected evidence, stale-fact rate from corpus stale
   labels, temporal accuracy from temporal-query evidence, cross-scope
   leakage from corpus/query scopes, and unsupported-answer rate from
   answerable/negative query semantics;
4. emits a raw per-query trace so release evidence can be audited.

The runner does not embed any external SDK; concrete hosted and
self-hosted adapters live in their own modules (e.g.
`severian.benchmarks.adapters.hosted`, `...self_hosted`) and depend
on their own optional dependency groups. The default CLI/SDK
neither imports nor references these adapters.

## Release evidence checklist

For every release candidate the release engineer MUST attach:

1. The manifest SHA (or pinned path under `docs/benchmarks/`).
2. The benchmark runner invocation and exit code, with concrete
   adapter registration. The manifest can be validated with:
   `python -m severian.benchmarks.cli validate --manifest <path>`.
   A benchmark run must use `BenchmarkRunner` with one adapter per
   manifest provider and must include the raw output below.
3. The raw `BenchmarkResult` JSON, including raw per-query traces.
4. The gate evaluation, computed from the JSON, with the actual
   metric values per provider.
5. The corpus version, model id, prompt version, configuration
   fingerprint, backend identity, candidate commit SHA and provider
   deployment metadata.

A release cannot be approved if any of these items is missing.

## Open items (deliberately deferred)

- Concrete hosted and self-hosted adapter implementations are
  deliberately separate from the core package. They must implement
  `ProviderAdapter`, pass query scope through to the provider, return
  explicit `ProviderQueryResponse.cost_usd`, and produce evidence fields
  sufficient for the required metrics before a release benchmark is
  considered valid.
