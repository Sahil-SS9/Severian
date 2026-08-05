# Severian auditor correction and implementation handover

**Date:** 2026-08-04
**Audience:** Original Severian authoring agent
**Status:** `NEEDS CORRECTION`
**Authority:** This packet reconciles the independent auditor, the delegated reviewer, and KENSEI's direct verification. Where they conflict, the direct primary-source verification recorded here controls.

## 1. How to use this handover

Work from a new isolated branch based on the verified repository state below. Do not edit `main` directly. Apply the code and documentation corrections in the stated order, generate new evidence only after the harness defects are fixed, and return the branch for independent review.

This is a correction lane, not permission to make the release gate green by changing thresholds or rewriting historical evidence.

### Verified starting state

- Repository: `/home/kensei/repos/Severian`
- Branch: `main`
- HEAD: `b628a97016e004398cfdb348e8b8b11e742b8168`
- Code baseline under the audit document: `dfc418f226e7705b4304ae30c9b720fc12ede210`
- `dfc418f..b628a97`: one added file only, `docs/benchmarks/auditor-handover-prompt-2026-08-04.md`
- Worktree at handover: clean
- Tests: `255 passed, 36 skipped, 3 warnings`
- Mypy: 46 source files clean under `--strict`
- Ruff: clean
- Both manifests: valid
- Fresh wheel: installs; CLI exposes `ingest`, `search`, `rollback`, and `episode`

The repository's core domain/repository behaviour is sound. The blocking work is benchmark isolation, batch-metric correctness, evidence provenance, and documentation reconciliation.

## 2. Locked findings — do not reinterpret

1. **Manifest-v1 does not pass.** Both Severian lanes report `gate_passed=false` because citation precision is `0.9375 < 1.0`. Recall is `1.000`; stale, leakage, and unsupported-answer rates are `0.000`.
2. **LOCOMO also does not pass.** Both Severian lanes reproduce recall `0.923077`, citation precision `0.65625`, stale `0.000`, temporal accuracy `1.000`, leakage `0.000`, and unsupported-answer rate `0.1875`.
3. **The 2026-08-02 evidence is historical and stale.** It reports recall `0.933`, citation precision `0.103`, and stale `0.159` SQLite/`0.145` PostgreSQL. Its result has no `git_sha`; its fingerprint records `27cf071`, and Git shows the evidence was last touched by `56b5dc9`.
4. **Committed LOCOMO evidence is not current-HEAD evidence.** Its fingerprint SHA is `75b61fc74196a9edac003d36e1abf382256fd0be`; the audited checkout was `b628a97`.
5. **Granite's committed trace does not prove q6 → c27 at rank 2.** The historical full-lane trace records q6 → `[]` for both Granite lanes. A current-HEAD bounded spot-check returned `c27` at rank 1. These are different evidence statements and must not be collapsed.
6. **The corrected batch command requires `--chroma-venv`.** Omitting it reproduces argparse exit code 2. Keep the mandatory flag in every command and handover.
7. **Do not rerun the 100k benchmark in this correction lane.** Verify and preserve its provenance only.
8. **A-3 remains blocked.** Do not fabricate a real-data result while corpus approval is absent.
9. **Do not lower release thresholds to turn failures into passes.** The gate output is the product finding.
10. **Do not overwrite historical evidence directories.** Preserve them and add explicit supersession/provenance notes or create new dated evidence after fixes.

## 3. Required correction register

### C-01 — P1: isolate Chroma benchmark state

**Problem**

The Chroma lane is not isolated between runs:

- `ChromaAdapter._collection_name()` ignores the adapter's configured prefix.
- Benchmark execution uses the persistent `/tmp/bench-chroma` store.
- The store is not cleared or uniquely namespaced per run.
- Fresh LOCOMO Chroma traces contained `d*` IDs from manifest-v1 and reported `cross_scope_leakage = 0.375`.

This means the cited external-provider lane is contaminated by prior benchmark corpora.

**Required changes**

- Update `src/severian/benchmarks/adapters/chroma.py` so collection identity includes a validated run/provider namespace as well as the scope hash.
- Update the benchmark construction path so every run receives a unique or explicitly cleared Chroma persistence root.
- Ensure cleanup is bounded to the benchmark-owned directory; never delete an arbitrary user path.
- Make corpus isolation independent of execution order.

**Required tests**

Add benchmark tests proving:

1. Two adapter prefixes cannot resolve to the same collection.
2. Running manifest-v1 then LOCOMO yields the same non-latency LOCOMO metrics as LOCOMO then manifest-v1.
3. Returned IDs are drawn only from the active manifest corpus; no `d*` IDs may appear in a LOCOMO-only run.
4. Cleanup/reset refuses any path that is not explicitly benchmark-owned.

Suggested location: `tests/benchmarks/test_chroma_isolation.py`.

**Acceptance evidence**

- Two clean LOCOMO runs in opposite order produce identical non-latency metrics and returned IDs.
- The Chroma lane contains no documents from another manifest.
- New evidence records the producing Git SHA and Chroma version.

### C-02 — P1: correct the batch speedup calculation

**Problem**

`chroma_batch_speedup_vs_perdoc` is inverted.

- Committed evidence stores `0.09`, but `22.8 / 2.1 = 10.86x`.
- Fresh evidence stores `0.11`, but `37.8 / 4.3 = 8.79x`.
- The current script calculates per-document throughput divided by batch throughput while labelling it batch speedup.

**Required changes**

- In `scripts/run_batch_compare.py`, calculate:

  `chroma_batch_docs_per_sec / chroma_per_doc_docs_per_sec`

- Add a zero-denominator guard.
- Rename or version the output field if downstream consumers may already rely on the inverted value.
- Add a deterministic unit test using fixed synthetic timings.

Suggested location: `tests/benchmarks/test_batch_compare.py`.

**Acceptance evidence**

- The stored field is greater than 1 when batch throughput is greater than per-document throughput.
- The stored value equals the displayed ratio within the declared rounding tolerance.
- Running without `--chroma-venv` still exits 2; running with it succeeds.

### C-03 — P1: make batch comparisons reproducible

**Problem**

The batch script uses fixed work directories and reuses document IDs. Reruns may measure updates/upserts against existing state rather than fresh insertion. Absolute throughput changed materially:

- Committed: Severian `184.4`, Chroma batch `22.8`, Chroma per-document `2.1` docs/s.
- Fresh: Severian `238.2`, Chroma batch `37.8`, Chroma per-document `4.3` docs/s.

The directional ordering held, but exact values did not reproduce.

**Required changes**

- Use unique benchmark-owned temporary roots or verified-clean run directories.
- Record warm-up policy, repetition count, sample timings, median, and spread.
- Record hardware/runtime metadata, dependency versions, producing Git SHA, corpus hash, and command arguments.
- Do not advertise a single absolute throughput value as generally reproducible without a tolerance and environment fingerprint.

**Acceptance evidence**

- At least three clean repetitions are retained.
- The report shows median and spread, not only one rounded number.
- A rerun cannot silently reuse a previous collection/database.

### C-04 — P1: correct the Granite q6 evidence statement

**Problem**

The handover claims the committed Granite trace proves `c27` at rank 2. It does not: the committed full-lane trace records q6 → `[]`. A current-HEAD spot-check loaded Granite successfully and returned:

- top three: `[c27, c12, c24]`
- `c27` rank: `1`

The historical Granite negative result is otherwise internally consistent: Granite recall `0.808/0.885` and unsupported `0.250`, versus hash recall `0.923` and unsupported `0.188`.

**Required changes**

- Correct `docs/benchmarks/auditor-handover-prompt-2026-08-04.md` and `docs/benchmarks/granite-comparison-2026-08-03.md` so they distinguish:
  - historical full-lane evidence: q6 empty and Granite does not beat hash overall;
  - current bounded spot-check: c27 rank 1.
- Do not edit the historical trace to match current behaviour.
- If retaining the spot-check as evidence, write a new dated artefact with command, current SHA, model identity, dependency versions, and result.
- Do not rerun the full Granite lane unless separately approved; it is outside this correction time-box.

### C-05 — P1: reconcile canonical gate documentation

**Problem**

Current documents contradict fresh execution. In particular, `docs/benchmarks/results.md` still claims manifest-v1 citation precision `1.000` and gate `PASS` for both Severian lanes. The actual value is `0.9375`, and both lanes fail.

**Required changes**

Update every active document that repeats the stale pass claim, including:

- `docs/HANDOFF-MASTER-2026-08-03.md`
- `docs/benchmarks/results.md`
- `docs/benchmarks/competitor-claims.md`
- `docs/plans/2026-08-03-complete-a-g-execution.md`
- `docs/benchmarks/auditor-handover-prompt-2026-08-04.md`

Use this canonical wording:

> Manifest-v1 reproduces recall 1.000, stale 0.000, unsupported 0.000, and cross-scope leakage 0.000 for both Severian lanes. The release gate does not pass because citation precision is 0.9375 against a configured threshold of 1.0.

Do not describe the gate as green, quality-passing, release-ready, or functionally passed.

### C-06 — P2: repair handover identity and Git-history assertions

**Problem**

The auditor prompt expected HEAD `dfc418f`, but committing the prompt moved HEAD to `b628a97`. Its expected 12-commit log also omits `1c005e9`. A document cannot reliably hardcode its own eventual commit SHA.

**Required changes**

- Define `dfc418f` as the code baseline, not the expected document-containing HEAD.
- Verify that the audit checkout is a descendant of the baseline and that the intervening diff is limited to explicitly allowed audit/documentation files.
- Replace brittle exact-log assertions with ancestry and allowed-diff checks.
- If an immutable exact reference is required, use a signed/reviewed tag after the correction commits rather than self-referential prose.
- Update HANDOFF §6's stale branch/HEAD references.

### C-07 — P2: make evidence status explicit

**Problem**

Historical and current evidence are mixed without a clear authority rule.

**Required changes**

- Mark `docs/benchmarks/evidence/2026-08-02/` as historical/superseded in documentation without rewriting its files.
- State that LOCOMO evidence produced at `75b61fc` is historical relative to current main.
- Do not “restamp” an old result with a new SHA. Generate new evidence only by running the fixed harness at the producing commit.
- Every new evidence package must include result, traces, fingerprint, manifest/corpus hash, producing SHA, branch/tag, environment versions, and exact command.
- Treat fresh reproduction as authoritative when historical narrative conflicts with it.

### C-08 — P2: investigate LOCOMO ranking determinism

**Problem**

Fresh MRR was `0.807692` SQLite and `0.818681` PostgreSQL, versus committed `0.792308/0.801282`. The 1.5–1.7 percentage-point movement is not floating-point noise.

**Required changes**

- Reproduce the run repeatedly from clean state.
- Identify whether ranking ties, insertion order, database ordering, generated IDs, or another source causes movement.
- Add deterministic tie-breaking where rankings are otherwise equal.
- Define an evidence tolerance only after the source of variance is understood; do not relabel the current delta as float noise.

**Acceptance evidence**

- Repeated clean runs produce identical non-latency metrics and returned-ID order, or the remaining stochastic component is explicitly designed, seeded, measured, and documented.

### C-09 — P2: complete the documented-limitations checklist

HANDOFF §8 currently documents A-3, B-1, and the cold-derive curve, but it does not contain all six required items.

Add explicit entries for:

1. A-3 blocked.
2. B-1/LOCOMO gate failure.
3. Manifest-v1 citation-precision failure.
4. Cold-derive curve.
5. Vendor-claim policy: cited vendor results are not internal reproductions.
6. No-secrets verification and its bounded scan scope.

The vendor policy and no-secrets statements already exist elsewhere; add them to §8 rather than claiming the checklist is complete.

### C-10 — P2: reconcile stale plan and competitor status

- Update `competitor-claims.md`; it still describes measured lanes as pending/unmeasured.
- Update the A–G execution plan's stale `IN PROGRESS` and pending wording.
- Correct LOCOMO analysis figures that conflict with the definitive/fresh MRR values.
- Keep vendor-reported claims clearly separated from independently reproduced lanes.
- Add hardware context to performance comparisons.

## 4. Files expected to change

### Code and tests

- `src/severian/benchmarks/adapters/chroma.py`
- `src/severian/benchmarks/runner.py` or its construction/configuration seam, if needed for unique run state
- `scripts/run_batch_compare.py`
- `scripts/run_benchmark.py`, only if required to pass an explicit isolated Chroma root/run ID
- `tests/benchmarks/test_chroma_isolation.py` — new
- `tests/benchmarks/test_batch_compare.py` — new
- Existing benchmark tests where the established structure is a better fit

### Active documentation

- `docs/HANDOFF-MASTER-2026-08-03.md`
- `docs/plans/2026-08-03-complete-a-g-execution.md`
- `docs/benchmarks/results.md`
- `docs/benchmarks/competitor-claims.md`
- `docs/benchmarks/locomo-tier-analysis-2026-08-03.md`
- `docs/benchmarks/granite-comparison-2026-08-03.md`
- `docs/benchmarks/auditor-handover-prompt-2026-08-04.md`

### Evidence

Create a new dated evidence directory only after C-01 through C-04 pass. Do not modify old result/traces/fingerprint files in place.

## 5. Implementation order

1. Create an isolated correction branch from `b628a97`; confirm it is clean.
2. Write failing tests for Chroma namespace/run isolation and the batch speedup ratio.
3. Fix C-01 and C-02; run the focused tests.
4. Make batch runs disposable and reproducible under C-03.
5. Investigate and resolve or explicitly bound C-08 ranking variance.
6. Run the complete static and test gates.
7. Generate fresh manifest-v1, LOCOMO, and batch evidence from the fixed commit.
8. Reconcile documents against the new primary evidence.
9. Run the handover commands exactly as a cold auditor would.
10. Submit code/tests, documentation, and new evidence as separately reviewable commits. Do not merge without KENSEI review.

## 6. Required verification commands

Use a securely supplied `SEVERIAN_PG_DSN`; do not hardcode or print credentials.

```bash
cd /home/kensei/repos/Severian
export PYTHONDONTWRITEBYTECODE=1

git status --short
git branch --show-current
git rev-parse HEAD

uv run pytest -q -p no:cacheprovider
uv run mypy --strict src/severian
uv run ruff check src/severian tests scripts

uv run python scripts/run_benchmark.py \
  --manifest docs/benchmarks/manifest-v1.json \
  --pg-dsn "$SEVERIAN_PG_DSN" \
  --chroma-venv /tmp/bench-compare-venv \
  --out /tmp/severian-correction-v1

uv run python scripts/run_benchmark.py \
  --manifest docs/benchmarks/manifest-locomo-tier-v1.json \
  --pg-dsn "$SEVERIAN_PG_DSN" \
  --chroma-venv /tmp/bench-compare-venv \
  --out /tmp/severian-correction-locomo

uv run python scripts/run_batch_compare.py \
  --chroma-venv /tmp/bench-compare-venv \
  --docs 200 \
  --out /tmp/severian-correction-batch
```

Also run the same manifest pair in reverse order against new clean run roots. Compare non-latency metrics and returned IDs. Any order-dependent result is a failed isolation gate.

### Expected semantic results

- Manifest-v1 remains an honest gate failure unless citation behaviour itself is fixed: citation precision `0.9375 < 1.0`.
- LOCOMO remains an honest gate failure on recall, citation precision, and unsupported answers unless those behaviours are intentionally fixed.
- Chroma results contain no documents from another manifest.
- `chroma_batch_speedup_vs_perdoc` equals batch throughput divided by per-document throughput.
- Historical Granite and 100k evidence remain historical; neither is silently reclassified as current reproduction.

## 7. Final acceptance checklist

Do not return the correction as complete until all items are true:

- [ ] Chroma state is unique or safely cleared per benchmark run.
- [ ] Manifest execution order does not alter non-latency metrics or returned IDs.
- [ ] Batch speedup direction and label are correct and tested.
- [ ] Batch evidence includes repetitions, median/spread, environment, and producing SHA.
- [ ] Ranking determinism is proven or explicitly bounded with evidence.
- [ ] Fresh evidence was produced only after the harness fixes.
- [ ] Old evidence files remain unchanged and are clearly marked historical in active docs.
- [ ] No active document claims manifest-v1 passes.
- [ ] Granite historical evidence and current spot-check are described separately.
- [ ] HANDOFF §8 contains all six limitations/policies.
- [ ] A-3 is still labelled blocked.
- [ ] The 100k lane was not rerun.
- [ ] Tests, mypy, ruff, manifest validation, and wheel/CLI checks pass.
- [ ] No credentials were introduced.
- [ ] Final branch diff contains only scoped code, tests, docs, and new evidence.
- [ ] Independent review returns `READY` before merge.

## 8. Required return packet

Return to KENSEI with:

1. Branch name and commit list.
2. File-by-file change summary.
3. Focused RED/GREEN test evidence for C-01 and C-02.
4. Full pytest/mypy/ruff outputs.
5. Fresh result/traces/fingerprint hashes and producing SHA.
6. Manifest-order isolation comparison.
7. Batch repetitions, median/spread, and corrected speedup.
8. Documentation grep proving stale `PASS` claims are gone from active documents.
9. Final clean-worktree evidence.
10. Explicit remaining gaps; do not report `READY` yourself.

## 9. Auditor reconciliation note

Both the delegated reviewer and KENSEI returned `NEEDS CORRECTION` and agreed on the core gates, stale provenance, and documentation drift. KENSEI's direct verification controls the disputed points:

- PostgreSQL MRR drift is not accepted as float noise without determinism proof.
- The Granite historical trace does not contain the claimed q6 rank-2 result.
- Chroma cross-run contamination is a real harness defect, not merely documentation drift.
- The batch speedup field is mathematically inverted.
- HANDOFF §8 does not yet document the manifest-v1 citation failure, vendor policy, and no-secrets check as required.

The correction is complete only when those controller findings are fixed and independently re-audited.
