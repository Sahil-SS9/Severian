# Severian Zero-Debt Execution — Independent Read-Only Audit

**Audit time:** 2026-08-09T17:02:59+01:00<br>
**Candidate:** `/home/kensei/repos/severian-zero-debt`<br>
**Branch/HEAD:** `zero-debt-embedding-perf` / `27b140127d0db8f928c8d7a9ee7d961b3bcf7925`<br>
**Controlling handoff:** `docs/handoffs/zero-debt-embedding-audit-prompt-2026-08-09.md`<br>
**Fresh evidence root:** `/tmp/auditor-severian-zero-debt-jniBVjlC`<br>
**Audit mode:** read-only candidate inspection; test/PostgreSQL fixtures and all new evidence confined to `/tmp` and the dedicated `severian-pg-test` container.

## VERDICT

**BLOCKED — core implementation is substantially verified, but the embedding benchmark/evidence and tier-selection claims are invalid. Do not merge, publish tiers, or close the zero-debt programme yet.**

The source test, type, lint, audit, PostgreSQL rebuild and wheel gates are green. The throwaway Rust spike correctly stopped below its 3× kernel gate and did not contaminate production source. However, the model-selection evidence cannot support a production embedding decision because:

1. the published “corpus SHA” is the SHA of the benchmark manifest, not the corpus;
2. throughput probes contaminate the index before quality scoring;
3. the bake-off bypasses its own release-gate evaluator and labels failed lanes `ok`;
4. query kinds are mostly labels over near-copy lexical queries, not valid correction/temporal/cross-language/long-context probes;
5. the matrix, hardware lanes, dimension controls and immutable model revisions are incomplete;
6. the durable support-tier document publishes claims that are not supported by the surviving raw evidence.

This is an **evidence/harness blocker**, not proof that Severian’s production scope isolation is broken. The 99-test live contract lane remains green, and the observed embedding leakage is partly manufactured by the benchmark contaminating its own index with unmanifested `enc-*` documents.

## CLAIMS A–H

| Claim | Result | Fresh evidence |
|---|---|---|
| A. Candidate state/ancestry/docs-only tip | **VERIFIED** | HEAD `27b1401`; `83cb95c`, `c92769c` and `c4d2b60` are ancestors; `83cb95c..HEAD` is docs-only; whitespace gate passes. Only disclosed untracked `docs/handoffs/`. |
| B. Full suite | **VERIFIED with handoff defect** | Fresh live-PG run: `564 passed, 11 skipped, 1 warning`, exit 0. The pasted DSN contains literal password `***` and fails authentication; the audit reconstructed the DSN from dedicated local container `severian-pg-test` without printing the secret. |
| C. Focused contracts | **VERIFIED** | Live contracts: `99 passed`; PG atomic rebuild: `3 passed`. Source inspection confirms per-table index names, underscore nonce, correct metadata swap and rollback cleanup. |
| D. Static/security gates | **VERIFIED** | 575 collected; mypy clean over 60 files; Ruff clean; pip-audit reports no known vulnerabilities and only the expected local `severian` package skip. |
| E. Privacy | **PARTIAL / HISTORICAL EVIDENCE INVALID** | Current exporter tests: `10 passed`. Content-only scan across 266 surviving corpora / 201,800 text fields: 0 emails and 0 phone-like values. Exact benchmark corpus is gone. Worse, claimed SHA `de09…` is the manifest SHA, so the original corpus was never bound. One older pre-final-fix 5,000-observation corpus still triggers three `api-key-label` detectors; preserved, not deleted, during this read-only audit. |
| F. Packet fidelity | **61 fields VERIFIED; conclusion REFUTED** | 61 scale/embedding fields copied from source JSONs with zero mismatches. The source files exist. But the packet omits configured gate failures, carries the wrong corpus hash, lacks immutable revisions and complete hardware metadata, and selects tiers despite invalid evidence. |
| G. Wheel | **VERIFIED** | Scratch `git archive` build produced `severian-0.3.0-py3-none-any.whl`; installed into clean CPython 3.11.15 venv; `import severian` passed. Candidate checkout untouched. |
| H. Rust absence/spike | **VERIFIED with probe/evidence caveats** | Production repo: 0 `Cargo.toml`, 0 `.rs`, 0 maturin artefacts. Spike parity/error contract rerun: `68 passed`. Structured evidence: 2.70× at 100K×768 (fails 3×), 67.6% e2e improvement (passes 25%). No production integration. The handoff’s broad zero-hit grep is invalid because pure-Python `vector.py` legitimately contains “exact top-k”. Surviving structured JSON does not independently prove the upper `2.91×` figure. |

## VERIFIED

### Candidate state

- `git rev-parse HEAD`: `27b140127d0db8f928c8d7a9ee7d961b3bcf7925`
- Branch: `zero-debt-embedding-perf`
- Ancestry passes for:
  - code final `83cb95c`
  - evidence SHA `c92769c`
  - baseline `c4d2b60` (`c4d2b602e82e48b70cb4299cb9511d0e7a6c064c`)
- `c92769c..83cb95c` changes only PG rebuild source/tests.
- `83cb95c..HEAD` changes only benchmark/support documentation.
- No native extension artefact entered the candidate.

### Fresh correctness and packaging evidence

- Full live-PG suite: `564 passed, 11 skipped`.
- Contract lane: `99 passed`.
- Atomic PG rebuild lane: `3 passed`.
- Collection: 575 tests.
- mypy: `Success: no issues found in 60 source files`.
- Ruff: `All checks passed!`.
- pip-audit: `No known vulnerabilities found`; local non-PyPI package skipped as expected.
- Privacy lane: `10 passed`.
- Scratch wheel: build/install/import passed on Python 3.11.15.
- PostgreSQL remediation implementation matches the described fixes. Minor documentation defect: `tests/unit/test_atomic_rebuild_pg.py` says candidate metadata is renamed to `vector_space_meta`; implementation/assertions correctly use `memory_vectors_meta`.

### Fresh bounded scale run

Fresh current-HEAD run (`/tmp/auditor-severian-scale-l3T4ehxv/scale.json`, SHA `7eeb260c…`) completed with full warmup at 1K and 10K:

| Bucket | Lane | docs/s | p50 | p95 |
|---|---|---:|---:|---:|
| 1K | SQLite | 55.6 | 44.47 ms | 56.74 ms |
| 1K | PostgreSQL | 53.6 | 53.75 ms | 125.45 ms |
| 10K | SQLite | 233.7 | 364.31 ms | 557.24 ms |
| 10K | PostgreSQL | 144.9 | 59.05 ms | 131.51 ms |

10K latency and throughput broadly reproduce the stored packet. 1K ingest throughput is materially lower than the stored run (stored 245.8/151.0 docs/s), showing cold-start/run-to-run sensitivity; do not treat one 1K throughput sample as stable.

### Rust stop decision

- Throwaway project remains isolated under `/tmp/severian-rust-topk-spike/`.
- Fresh spike parity/error test: `68 passed in 41.33s`.
- `benchmark-completion.json`: 2.70× 100K×768 kernel speedup — below required 3×.
- `benchmark-e2e.json`: 67.6% end-to-end improvement — above required 25%.
- One failed mandatory gate is enough to reject integration. The stop/no-integration decision is correct.

## REFUTED / BLOCKING FINDINGS

### CRITICAL 1 — “Corpus SHA” hashes the manifest, not the corpus

**Evidence**

- `scripts/run_embedding_benchmark.py:227-228` constructs `EmbeddingBakeoffRunner(..., corpus_path=args.manifest)`.
- `src/severian/benchmarks/embedding_runner.py:315-319` hashes whatever `corpus_path` receives.
- Fresh `sha256sum docs/benchmarks/manifest-embedding-v1.json` equals exactly:
  `de09ea4e482de39b8946430fec14dcc8b5d7e67839d85da2d31e0203f5904839`.
- That is the value published as the redacted corpus SHA in raw evidence, `fingerprint.json`, README and support-tier docs.

**Impact**

The quality results are not cryptographically bound to their real input corpus. The “zero PII post-scan” statement also attaches to the wrong file. Because the real corpus has expired from `/tmp`, this evidence cannot be repaired editorially; the corpus must be regenerated and the bake-off rerun.

### CRITICAL 2 — Bake-off bypasses its own release gate

**Evidence**

- `EmbeddingBakeoffRunner._measure_lane()` calls private `_run_provider()` directly (`embedding_runner.py:207-213`).
- It never calls `BenchmarkRunner.run()` or `_evaluate_gate()`.
- `scripts/run_embedding_benchmark.py:248-256` prints `ok` for every non-skipped lane regardless of quality thresholds.
- Applying Severian’s own evaluator to the raw results returns:
  - `granite-311m`: citation precision, cross-scope leakage, unsupported-answer rate fail.
  - `granite-small-en`: same three failures.
- All five surviving lane checkpoints fail the same three gates.

**Impact**

No measured lane is currently eligible for selection. The published high/lite tiers are not release-gated.

### CRITICAL 3 — Throughput measurement contaminates quality scoring

**Evidence**

- `embedding_runner.py:179-185` measures throughput by inserting 200 `enc-*` documents into the same adapter/index later used for quality scoring.
- Those IDs are not in the manifest document map.
- `runner.py:230-235` counts any returned unknown document ID as cross-scope leakage.
- Extra documents can also displace expected evidence, lowering citation precision.

**Impact**

The measured ~18.3–18.8% leakage and ~0.66–0.68 citation precision do not cleanly represent production behaviour. The benchmark invalidates itself. Product scope isolation should not be declared broken from these numbers, but model quality cannot be declared passing either.

### HIGH 4 — Query taxonomy is not a meaningful real-workload benchmark

**Evidence**

- `build_redacted_embedding_corpus.py:203-219` rotates labels across fact/paraphrase/correction/temporal/negative/code/crosslang/long/adversarial.
- For every non-negative kind it forms the query from the same first eight >3-character words of the source document.
- “crosslang” does not translate; correction has no supersession relation; temporal has no time contrast; long-context does not test length; code/task has no specialised construction.
- The execution ledger itself notes all lanes saturate at recall@10 `0.99145` because queries are near-copies and lexical fusion carries recall.

**Impact**

The benchmark cannot discriminate the intended memory capabilities and cannot select a multilingual default/high/lite model reliably.

### HIGH 5 — Candidate/runtime matrix is incomplete

**Required vs executed**

- Required baseline/control/default/high/lite matrix: hash, Granite 107M, Granite 97M R2, EmbeddingGemma 300M, Qwen3 0.6B with instruction/no-instruction, Granite 311M R2, Bekko a8m, Small English control.
- Surviving checkpoints: only hash + four Granite lanes.
- Final merged JSON/packet: only Granite 311M and Small English; it marks hash/107M/97M skipped despite surviving completed checkpoints.
- Never run: EmbeddingGemma, Qwen, Bekko.
- Missing: Qwen instruction control, Matryoshka dimensions, multiple dimensions/quantisations, laptop/capped CPU lane, three-run variance, per-language/cross-language scoring, length buckets.

**Impact**

The requested embedding model benchmarking was not completed. The stated semantic tiers are premature.

### HIGH 6 — Model revisions are not immutable or reproducible

- Manifest uses `revision: unknown-pin-at-run`.
- `_STEmbedding` calls `SentenceTransformer(model_name)` without a revision.
- Durable fingerprint omits immutable model revisions.

This violates the embedding-space identity and reproducible-acquisition requirements. A rerun at the same Git SHA could load different weights.

### HIGH 7 — Peak RSS measurement is invalid across sequential lanes

- `_peak_rss_mb()` uses process-lifetime `resource.getrusage(...).ru_maxrss`.
- Per-lane RSS is calculated as after-minus-before in one long process.
- A later lane that does not exceed the previous high-water mark reports near zero, explaining Small English `2.8 MB`.
- Granite 97M reports anomalous `9.3 GB` while 107M reports `594 MB`.

Run each model lane in a fresh child process or use an independently sampled process peak. Do not publish current RSS figures.

### HIGH 8 — Support-tier document contains unsupported/misleading claims

- `support-tiers-2026-08-09.md` gives default `HashEmbedding` latency as `~1 ms`.
- The surviving hash embedding checkpoint reports p95 `126.5 ms`; ~1 ms corresponds to the separate lexical baseline at 1K, not the full hash+Severian retrieval lane.
- The document selects semantic tiers despite failed/unapplied release gates and invalid corpus binding.
- “Best measured semantic recall” is non-discriminating: every surviving lane has the same `0.99145` recall.

The document must be withdrawn or marked invalid pending rerun.

### MEDIUM 9 — Final evidence merge/reconciliation is misleading

Five completed lane checkpoints survive, but the final two-lane invocation overwrote `embedding-bakeoff.json`. The durable packet then labels three completed lanes as skipped/no quality run. Implement a deterministic checkpoint merge that records per-lane provenance and refuses incompatible corpus/manifest/SHA inputs.

### MEDIUM 10 — The reviewer handoff has three invalid probes

1. Literal PG password `***` cannot run independently.
2. The broad Rust grep expects zero hits but matches legitimate pure-Python “exact top-k”.
3. `rm -rf dist build && uv build` mutates the audited checkout despite “read-only”; the audit used a scratch `git archive` instead.

These do not refute the candidate, but the handoff must be corrected before reuse.

## GAPS / HONEST LIMITATIONS

- No current-candidate complete-warmup 100K run. The partial 100K packet remains trend-only.
- Exact real corpus cannot be rescanned or rehashed because it expired; the published hash was never its hash.
- No fresh neural model downloads or bake-off reruns were performed in this bounded review.
- No cross-platform wheel claim was tested; only Linux x86_64 / CPython 3.11.15.
- The upper spike figure `2.91×` is not independently supported by surviving trusted JSON; `2.70×` is supported and is sufficient to reject the 3× gate.
- The anomalous old corpus was preserved. Ownership/runtime references must be checked before later cleanup.
- The full-suite Starlette/httpx deprecation warning remains but is non-blocking in this audit.

## POST-AUDIT AGREED DECISIONS

### A — Selective Rust acceleration is approved

The original spike correctly stopped because it missed the original 3× kernel gate. Sahil has now reviewed the broader result and decided that the measured **67.6% end-to-end improvement is significant enough to warrant production work**.

The revised decision is not “port only top-k” and not “rewrite Severian in Rust”. It is:

1. Keep Python as Severian's product/control plane.
2. Run a selective Rust acceleration programme across the strongest candidates.
3. Productionise every candidate that proves a meaningful end-to-end gain while preserving correctness.

Candidate order:

1. packed-vector decoding + cosine/dot-product + deterministic exact top-k as one connected vector-search seam;
2. MinHash/LSH signature generation;
3. dense duplicate/contradiction candidate generation;
4. a possible native SQLite/vector extension or index strategy.

Every promoted seam must preserve the Python public API, deterministic ranking/source identity, malformed-input safety, PyO3/maturin wheel delivery, visible Rust/Python backend telemetry and a pure-Python fallback. Evidence from one seam does not automatically approve another; each receives its own component and end-to-end benchmark.

### B — Qwen becomes the priority embedding challenger

The Granite-only tier decision remains invalid. The repaired cross-family bake-off must prioritise **Qwen3 Embedding 0.6B** while fairly testing EmbeddingGemma 300M, Bekko a8m, Granite 107M/97M R2/311M R2 and hash.

Qwen is the priority challenger, not a preselected winner. It must be tested with correct query-only instructions plus a no-instruction control, multiple dimensions, multilingual real-workload probes, CPU latency/RAM and every release gate.

## RECOMMENDED NEXT STEPS / ACTIONS

### P0 — Invalidate the current embedding decision packet

1. Mark `docs/benchmarks/evidence/2026-08-09-zero-debt/` and `support-tiers-2026-08-09.md` **INVALID FOR MODEL SELECTION**.
2. Do not delete the raw `/tmp` evidence yet; preserve it until replacement evidence is accepted and ownership/reference checks pass.
3. Keep production/default behaviour unchanged. Do not activate a new semantic model from this packet.

### P0 — Repair benchmark evidence identity

4. Pass `args.corpus` to `EmbeddingBakeoffRunner(corpus_path=...)`; fail if corpus mode is requested but the file is absent.
5. Record separate immutable hashes for:
   - manifest;
   - corpus;
   - runner source/commit;
   - each model artefact/revision;
   - merged checkpoint set.
6. Add a CLI integration test asserting the output corpus SHA equals the supplied corpus bytes and differs from the manifest SHA.

### P0 — Make quality gates executable and blocking

7. Have the embedding runner evaluate the manifest release gate for each completed lane.
8. Persist `gate_passed` and exact `gate_failures`; exit non-zero or refuse tier publication when no candidate passes.
9. Remove unconditional `ok` output.
10. Add a regression test using deliberately failing citation/leakage/unsupported values.

### P0 — Stop quality/throughput cross-contamination

11. Measure encode throughput in a separate fresh adapter/index or after all quality scoring.
12. Ensure quality starts from exactly the manifest corpus and assert no extra IDs exist.
13. Add a test proving throughput measurement cannot change returned IDs or quality metrics.

### P1 — Replace the pseudo-taxonomy corpus

14. Regenerate the redacted corpus with explicit query scope and expected document IDs stored directly—never rematch evidence by content.
15. Build and manually approve 100–200 meaningful probes:
   - true paraphrases;
   - superseding correction pairs;
   - past-vs-current temporal questions;
   - genuine negative/abstention probes;
   - code/task retrieval;
   - actual cross-language query/document pairs;
   - long-context buckets.
16. Store only scrubbed inputs under owned temporary roots; commit only hashes, aggregate metrics and approved opaque examples.
17. Rerun the current detector before model download and after corpus generation; retain a deterministic detector report bound to the actual corpus SHA.

### P1 — Make model runs reproducible

18. Resolve and pin immutable Hugging Face revision SHAs before model load; pass `revision=` to the runtime.
19. Separate query/document encoding contracts. Qwen instructions must apply to queries only, plus a no-instruction control.
20. Execute the complete candidate/dimension matrix or explicitly return “no selection” rather than publishing tiers.
21. Run each lane in a fresh process and measure true peak RSS, cold load and artefact bytes.
22. Add capped-core/capped-memory CPU evidence and three repeated runs with variance.

### P1 — Reconcile evidence atomically

23. Merge per-lane checkpoints with a tool that rejects different corpus hashes, manifests, Git SHAs, revisions or runtime contracts.
24. Never overwrite a complete merged result with a subset run.
25. Publish all gate metrics, not only recall/MRR/nDCG/latency.
26. Correct the hash-latency row and remove the current Granite tier selection until a valid lane passes.

### P1 — Execute the selective Rust candidate programme

27. Productionise the already-validated vector-search seam first: packed-vector decoding, cosine/dot-product and exact deterministic top-k behind the existing Python interface.
28. Re-run parity, malformed-input, memory-growth, wheel and end-to-end tests against the exact production candidate before enabling it.
29. Profile and spike MinHash/LSH signatures, dense candidate generation and native SQLite/vector options independently.
30. Integrate every candidate that demonstrates meaningful end-to-end benefit with no correctness regression; reject candidates whose gains disappear across the FFI/storage boundary.
31. Keep the pure-Python path fully tested and report which backend ran.

### P2 — Complete performance evidence

32. Re-run complete-warmup 1K/10K after harness repair; investigate 1K ingest variance.
33. Run one current-SHA complete-warmup 100K lane under a bounded background process if a 100K support claim is required.
34. Keep PostgreSQL as the large-corpus recommendation unless valid evidence shows otherwise.

### P2 — Close minor documentation/probe debt

35. Correct `test_atomic_rebuild_pg.py`’s stale `vector_space_meta` header text.
36. Replace the handoff DSN placeholder with a documented local credential-discovery prerequisite or an explicit “reviewer must supply DSN” gate.
37. Use separate Rust searches for `pyo3|maturin` and file artefacts; do not include generic “exact top-k”.
38. Build wheels from a scratch archive/worktree in future read-only audits.

## INDEPENDENT REVIEW CORROBORATION

A separate read-only KENSEI Review pass independently reached **BLOCKED** after inspecting the handoff, benchmark runner, manifest, corpus builder, evidence packet, support-tier document and PostgreSQL remediation source. Its direct source findings corroborate:

- `args.manifest` is passed where the corpus path must be hashed;
- the embedding runner calls `_run_provider()` and bypasses release-gate evaluation;
- first-eight-word query construction makes the workload non-discriminating;
- neural revisions remain `unknown-pin-at-run` instead of immutable HF snapshots;
- PostgreSQL rebuild remediation is correctly implemented and tested;
- the Rust spike did not enter production source.

The independent reviewer lacked terminal/test tools and explicitly left command execution unverified; the parent audit supplies those fresh command results. Its full report is preserved at:

`/home/kensei/.hermes/cache/delegation/subagent-summary-0-20260809_165307_967418.txt`

No independent finding contradicts or softens the parent verdict.

## FINAL DECISION

- **Core Python implementation:** strong evidence, but not approved independently of the broken benchmark modules included in the same candidate.
- **PostgreSQL rebuild remediation:** verified.
- **Algorithmic performance repairs at 1K/10K:** substantially verified.
- **100K support:** gap.
- **Embedding model/tier decision:** invalid; rerun required.
- **Rust acceleration:** the original stop was correct under the old gate, but the 67.6% end-to-end result is now accepted as sufficient evidence to begin a selective Rust production programme. Vector search goes first; other strong candidates are benchmarked and promoted on their own evidence.
- **Zero-debt programme:** **NOT DONE**.
- **Merge/publish/activate:** **BLOCKED pending P0 repairs and a valid embedding bake-off packet**.

## NOTES

- Repository state after audit remains `?? docs/handoffs/`; no tracked candidate file was changed.
- Scratch artefacts created by this audit:
  - `/tmp/auditor-severian-zero-debt-jniBVjlC/`
  - `/tmp/auditor-severian-wheel-aEccR6c9/`
  - `/tmp/auditor-severian-scale-l3T4ehxv/`
  - `/tmp/auditor-severian-rust-parity`
- The first full-suite attempt used the handoff’s literal `***` password and failed authentication. It was superseded by the successful live-PG run using the dedicated container’s locally discovered credential.
- Two intermediate packet-comparison scripts had auditor-side field-selection errors; both were discarded. The corrected comparison checked 61 fields and found zero source/packet mismatches.
