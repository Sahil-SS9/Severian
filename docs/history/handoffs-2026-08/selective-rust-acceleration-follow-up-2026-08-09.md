# Severian Selective Rust Acceleration — Follow-up Implementation Plan

> **Status:** Approved direction, implementation not started. This document supersedes only the old “do not productionise Rust” stop decision. It does not alter the independent audit findings or approve merge/activation.
>
> **For the Severian builder:** execute in the existing isolated candidate or a new isolated descendant. Use RED → GREEN → REFACTOR. Do not edit canonical `main`, production stores, Hermes configuration, services or gateways. Return a reviewed candidate; do not merge, push or activate it.

**Goal:** Productionise every worthwhile Rust acceleration seam, beginning with the proven local vector-search path, while Python remains Severian's control plane and pure-Python installation/runtime remains fully supported.

**Architecture:** Keep all domain, scope, persistence, embedding, correction and orchestration rules in Python. Put only measured CPU-heavy kernels behind PyO3/maturin. Ship native code as an optional companion wheel so the existing Hatchling-built `severian` package remains installable without Rust. Every native seam has an exact Python reference, an explicit backend selector and its own performance disposition.

**Primary evidence:**

- Independent consolidation: `docs/handoffs/zero-debt-independent-audit-consolidation-2026-08-09.md`
- Original executed plan: `/home/kensei/repos/Severian/.hermes/plans/2026-08-08_152340-severian-zero-debt-embedding-performance-rust-spike.md`
- Throwaway spike: `/tmp/severian-rust-topk-spike/`
- Trusted structured spike results:
  - `/tmp/severian-rust-topk-spike/benchmark-completion.json`
  - `/tmp/severian-rust-topk-spike/benchmark-e2e.json`
- Current production seam: `src/severian/infrastructure/vector.py:337-415`

---

## 1. Locked decisions

| Decision | Locked choice |
|---|---|
| Product/control plane | Python remains authoritative. No full Rust rewrite and no Go rewrite. |
| Rust programme | Evaluate all credible native candidates and integrate every one that proves meaningful end-to-end value. Do not port cold paths for appearance. |
| First production seam | Packed f32le decode + exact dot/cosine scoring + deterministic top-k is one connected seam and proceeds first. |
| Existing result | 2.70× trusted 100K×768 kernel speedup and 67.6% 10K×384 end-to-end improvement. Sahil accepts the end-to-end result as sufficient to proceed despite the old 3× kernel gate. |
| Public interface | `LocalVectorIndex.search(scope, query, *, limit)` and `VectorHit` behaviour remain unchanged. |
| Ranking | Exact deterministic key remains `(score DESC, content ASC, memory_id ASC)`. Source-distinct memories remain distinct. |
| Fallback | Pure Python remains fully installable and tested. Native absence must never make base Severian unusable. |
| Backend selection | Support `auto`, `python`, and `rust`. `rust` fails closed when unavailable; `auto` may select Python but must expose the selected backend in diagnostics/telemetry. |
| Native packaging | Use PyO3/maturin as a separate optional companion package/wheel; do not replace the root Hatchling build backend. |
| SQLite/domain boundary | Rust receives already scope-filtered rows. It does not own SQLite connections, scope filtering, model loading, canonical writes, corrections, provenance or queue logic in the first production slice. |
| Other candidates | Re-profile after the vector seam. Promote only candidates that remain material; preserve evidence-backed DEFER/REJECT dispositions. |
| Embedding relationship | Rust kernels must be dimension-agnostic and tested at 256/384/512/768/1024. Final end-to-end evidence is repeated for the embedding model selected by the repaired cross-family bake-off, prioritising Qwen3 Embedding 0.6B. |
| Activation | Building and reviewing code does not authorise merge, default-backend change, production vector rebuild or runtime activation. |

---

## 2. Why this is a separate document

The embedding repair and Rust acceleration can run in one builder cycle, but their evidence must not be mixed:

- embedding work decides model quality, dimensions, memory and latency;
- Rust work decides whether a Python hot path should be replaced by a native kernel;
- a model can win while Rust fails, and Rust can win while no model passes;
- each track therefore returns its own gates and then one combined release packet.

The builder may implement both tracks in the same isolated branch. It must preserve separate commits, tests, evidence directories and verdicts.

---

## 3. Current candidate ladder

| Rank | Candidate | Current disposition | Required next action |
|---:|---|---|---|
| 1 | Packed-vector decode + exact score + deterministic top-k | **PROMOTE NOW** | Productionise the proven spike through the existing Python interface. |
| 2 | MinHash/LSH signature generation | **RE-PROFILE, NOT PRE-APPROVED** | Python repairs removed it as a dominant cost. Profile current code; spike only if it again consumes a material share. |
| 3 | Dense duplicate/contradiction candidate generation and exact Jaccard | **RE-PROFILE, NOT PRE-APPROVED** | Condensation and batching removed the prior degeneration. Profile current diverse/dense corpora before any Rust code. |
| 4 | Native SQLite vector/index strategy | **RESEARCH ONLY** | `sqlite-vec` v0.1.9 remains rejected because LIMIT-k ties return an arbitrary subset. Evaluate another strategy only if exact local search remains a support-tier bottleneck after candidate 1. |
| 5 | Embedding inference in Rust | **OUT OF SCOPE** | ONNX/OpenVINO/sentence-transformers already own optimised inference. Do not create a redundant wrapper without a measured gap. |

“Maximise the best candidates” means exhaustively profiling this ladder and promoting every winner—not implementing every row regardless of evidence.

---

## 4. Target file layout

### Base Python package

- Modify: `src/severian/infrastructure/vector.py`
- Create: `src/severian/infrastructure/native_accel.py`
- Create: `tests/unit/test_native_vector_backend.py`
- Modify: `tests/unit/test_bounded_topk.py`
- Modify: `tests/unit/test_vector_blob_storage.py` only if the canonical scoring contract needs explicit cross-version coverage
- Create: `tests/integration/test_native_vector_integration.py`
- Create: `tests/gates/test_native_fallback_contract.py`

### Optional native companion package

- Create: `native/pyproject.toml`
- Create: `native/Cargo.toml`
- Create: `native/src/lib.rs`
- Create: `native/README.md`
- Create: `native/tests/` only for Rust-native tests that cannot be expressed through the Python public contract

Suggested distribution/module names:

- distribution: `severian-native`
- Python module: `severian_native`
- exported first function: `exact_top_k(query, rows, k)`

### Build, CI and evidence

- Modify: `.github/workflows/ci.yml`
- Create: `scripts/benchmark_native_acceleration.py`
- Create: `tests/benchmarks/test_native_acceleration_runner.py`
- Create after execution: `docs/benchmarks/evidence/<DATE>-rust-acceleration/README.md`
- Create after execution: `docs/benchmarks/evidence/<DATE>-rust-acceleration/fingerprint.json`
- Create after execution: `docs/benchmarks/evidence/<DATE>-rust-acceleration/result.json`

Do not copy `/tmp/severian-rust-topk-spike/target/`, its venv, caches or shared `benchmark.log` into Git. Port only reviewed source/tests and trusted structured findings.

---

# Phase R0 — Freeze contracts and reproduce the accepted result

## R0.1 Establish candidate identity

Record:

- base and candidate SHAs;
- branch/worktree;
- pre-run porcelain including untracked files;
- Python/Rust/maturin versions;
- CPU architecture, core count and memory;
- exact spike file hashes.

Do not delete or “clean” the existing `/tmp` spike. It is historical evidence whose ownership/references must be preserved until replacement evidence is accepted.

## R0.2 Lock the cross-version score contract

The spike was built and tested on CPython 3.12, while Severian supports Python 3.11+.

Before production code:

1. Add a RED test comparing the Python reference and Rust scoring on Python 3.11 and 3.12.
2. Define one version-independent score contract. Preferred: use `math.fsum(left * right ...)` in the pure-Python reference and the same compensated summation in Rust.
3. Prove the deliberate Python-reference change does not alter expected IDs/order on the full existing ranking suite and benchmark manifests.
4. Do not branch scoring semantics by Python version; that would make indexes rank differently across runtimes.

## R0.3 Reproduce the accepted vector evidence

Use dedicated output files—never the shared spike `benchmark.log`.

Required baseline:

- parity/error contract: 68/68 or expanded equivalent;
- trusted 100K×768 kernel result near the historical 2.70× range;
- trusted 10K×384 end-to-end result showing at least 25% improvement;
- no repeated-call memory growth;
- no panic/segfault on malformed input.

If the result cannot be reproduced, stop productionisation and return the discrepancy. Do not tune tests or discard slow runs.

---

# Phase R1 — Create the optional native companion safely

## R1.1 RED: base Severian works without native code

Write tests proving:

- importing `severian` succeeds when `severian_native` is absent;
- `backend=python` always uses the Python path;
- `backend=rust` raises a clear configuration error when native code is absent;
- `backend=auto` selects Python when native code is absent and reports `python` through diagnostics;
- no import exception is silently swallowed without backend state being observable.

Run the tests and preserve the failing output before implementation.

## R1.2 Create the maturin companion package

Port the reviewed spike kernel with production hardening:

- `#![forbid(unsafe_code)]`;
- no `.unwrap()` or panic-reliant input path;
- strict empty-query, dimension, malformed-BLOB and finite-value validation;
- no per-row decoded `Vec<f64>` allocation;
- GIL released around CPU work;
- exact deterministic total ordering;
- `k=0`, `k>N`, empty rows and all-tie rows defined explicitly;
- architecture flags chosen for parity, not host-only benchmark inflation;
- no SQLite, scope, model, provenance or domain logic.

Keep the root `severian` build as Hatchling. The companion package uses maturin independently, preserving a genuinely pure-Python base installation.

## R1.3 GREEN: native package contract

Required Python-level parity matrix:

- dimensions: 256, 384, 512, 768, 1024;
- rows: 0, 1, `k-1`, `k`, `k+1`, 1K, 10K and bounded 100K;
- `k`: 0, 1, 20, `N`, `N+1`;
- random vectors;
- identical vectors and exact score ties;
- repeated deterministic runs;
- malformed lengths;
- NaN/Inf in queries and rows;
- non-ASCII IDs/content;
- Python 3.11 and 3.12.

No tolerance that can change ranking is allowed. IDs and order must be exact; score representation must satisfy the canonical score contract.

---

# Phase R2 — Integrate through one Python adapter

## R2.1 Create `native_accel.py`

Responsibilities only:

- parse `SEVERIAN_VECTOR_BACKEND=auto|python|rust` or the equivalent constructor option;
- import the optional native module;
- expose selected backend and native version;
- call exact native top-k;
- normalise native exceptions to existing Severian errors;
- fail closed for explicit `rust` when unavailable/broken;
- provide no duplicate ranking implementation.

Do not put selection logic directly in `LocalVectorIndex.search()` beyond one adapter call.

## R2.2 Wire `LocalVectorIndex.search()`

Preserve the public method and scope-filtered SQL query. Flow:

1. Python embeds the query.
2. Python fetches rows for the exact scope.
3. Adapter selects Rust or Python exact-top-k.
4. Existing `VectorHit` objects are returned.

The Python reference remains the fallback and test oracle. The optional NumPy path must receive an explicit disposition:

- retain as a separately forced diagnostic backend, or
- remove only if Rust plus Python covers every supported need and tests prove no user-facing contract depends on it.

Do not leave three ambiguous “auto” accelerators racing silently.

## R2.3 Add visible backend diagnostics

At minimum expose:

- requested backend;
- selected backend;
- native module/version when selected;
- fallback reason when `auto` selected Python;
- call/failure counters suitable for tests and operational inspection.

Do not persist backend identity as embedding-space identity: acceleration backend must not alter vectors or ranking. It belongs in diagnostics/evidence, not the vector fingerprint.

---

# Phase R3 — Production gates for the vector seam

## Correctness gates

- Existing bounded-top-k tests pass unchanged.
- Native integration returns the same `VectorHit` IDs, order and canonical scores as Python.
- Scope isolation remains Python-owned and unchanged.
- No canonical-memory or projection writes are introduced.
- Explicit Rust mode fails closed on native failure; it never silently rewrites/falls back.
- Auto mode records any fallback.
- Python-only installation passes the complete suite.
- Native-installed environment passes the complete suite.

## Performance gates

The old 3× standalone-kernel gate is retired for this seam. Required production gate:

- at least 25% end-to-end p95 improvement over the strongest pure-Python baseline at representative 10K scopes;
- report 100K trend/complete evidence separately;
- report absolute p50/p95/p99 and relative improvement;
- include dimensions 384/512/768/1024;
- repeat each lane at least three times in fresh processes;
- include FFI conversion and SQLite row-fetch cost;
- no material regression for small scopes where FFI overhead may dominate;
- no unbounded RSS growth.

The target is to reproduce or improve upon 67.6%, but 67.6% is historical evidence—not a fabricated mandatory exact value.

## Packaging gates

- base pure-Python wheel builds/installs/imports on CPython 3.11 and 3.12;
- native wheel builds/installs/imports on Linux x86_64 for CPython 3.11 and 3.12;
- install native then uninstall native: base package still works;
- source checkout without Rust toolchain: Python tests still work;
- `pip-audit`, Cargo dependency audit and licence inventory recorded;
- do not claim cross-platform support from Linux-only evidence.

---

# Phase R4 — Re-profile and maximise the remaining Rust candidates

Run this only after R3 is green so profiles reflect the new vector path.

## R4.1 Current profile

Measure end-to-end share for:

- `_minhash_signature`;
- `_candidate_batches` bucket/pair generation;
- exact `_jaccard_tokens` over candidate pairs;
- tokenisation/content-digest work;
- SQLite row fetch versus native score/select;
- FFI conversion/copy overhead.

Use diverse, shared-template, exact-duplicate, contradiction-band, stopword-heavy and large bounded corpora. Record CPU, wall time, candidate ratio, exact recall and peak RSS.

## R4.2 Promotion rule

A second Rust candidate may proceed only when either:

- it consumes at least 15% of the relevant end-to-end workflow, or
- it causes a documented support-tier limit, and
- a bounded spike projects at least 20% end-to-end improvement or removes that limit.

Correctness remains absolute: duplicate/contradiction recall, deterministic ordering and source identity cannot regress.

## R4.3 Candidate-specific dispositions

### MinHash/LSH

- Re-profile the repaired implementation first.
- If still cold, return `DEFER — not currently material` with fresh numbers.
- If hot, spike signature generation only; Python keeps tokenisation policy, thresholds and semantic decisions unless moving a larger seam is proven necessary.

### Dense candidate/Jaccard

- Preserve exact-duplicate condensation and bounded deterministic batches.
- Do not cross FFI per pair; batch enough work to amortise conversion.
- Compare returned pair IDs/order/overlap exactly against Python.

### SQLite/native indexing

- `sqlite-vec` v0.1.9 remains rejected unless a newer implementation proves deterministic recovery of every tied boundary candidate.
- ANN is not a substitute for the exact default contract.
- A custom native storage/index seam requires a separate design review because it expands Rust into persistence and rollback authority.

Every row receives one final disposition: `PROMOTE`, `DEFER`, or `REJECT`, with fresh evidence. “Not implemented” is not a disposition.

---

# Phase R5 — Evidence, review and return package

## Durable evidence

Commit only structured, scrubbed evidence:

- exact code SHA and dirty-patch identity;
- Python/Rust/maturin versions;
- CPU/OS/runtime fingerprint;
- exact commands and seeds;
- baseline and native backend versions;
- dimensions, rows, k and repetitions;
- parity/error/memory results;
- raw-result hashes;
- p50/p95/p99 and end-to-end improvements;
- every skipped lane and reason;
- candidate dispositions.

Raw logs and large fixtures stay under an owned temporary root. Never reuse the untrustworthy shared `benchmark.log`.

## Required final gates

1. Focused RED/GREEN proof for each production seam.
2. Separate collection and full-suite execution.
3. Python-only full suite.
4. Native-installed full suite.
5. mypy, Ruff, Python compile and pip-audit.
6. Cargo fmt, clippy with warnings denied, Rust tests and dependency/licence audit.
7. SQLite plus live-PostgreSQL contracts.
8. CLI/SDK/API/MCP/Hermes integration regression.
9. Pure-Python and native wheel clean-venv installs.
10. `git diff --check` and post-test residue inspection.
11. Fresh independent architecture, QA and security review of the exact final candidate.

## Builder return format

```text
VERDICT
- Vector Rust seam: READY FOR INDEPENDENT REVIEW | BLOCKED
- Other candidate dispositions: PROMOTE | DEFER | REJECT per named candidate
- No merge/activation performed

CANDIDATE IDENTITY
- Base SHA, final SHA, branch/worktree, porcelain status

CHANGED PATHS
- Python adapter/integration
- Native companion package
- Tests/CI
- Evidence/docs

CORRECTNESS
- Python 3.11/3.12 parity
- IDs/order/scores
- malformed input
- scope/public-interface preservation
- Python-only fallback

PERFORMANCE
- Baseline/native p50/p95/p99
- end-to-end improvement by dimension/scale
- FFI/RSS
- repeated-run variance

PACKAGING
- Base and native wheel identities
- clean-venv install results
- unsupported platforms stated honestly

CANDIDATE LADDER
- vector seam
- MinHash/LSH
- dense candidate/Jaccard
- SQLite/native strategy
- evidence-backed disposition for each

TEST/QUALITY
- collection and execution exits/counts
- Python/Rust static/security gates
- independent reviews

SAFETY
- production/canonical stores untouched
- canonical checkout untouched
- no merge, push, activation or vector rebuild
- temporary artefacts accounted for
```

The builder must not self-declare overall READY. Independent review owns the final merge recommendation.
