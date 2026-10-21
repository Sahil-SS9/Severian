# Severian Zero-Debt Remediation + Selective Rust — Builder Handoff Prompt (2026-08-09)

> **How to use:** Copy everything below the line into a fresh chat with the Severian builder agent. The agent needs no previous chat context. It must read the referenced local files before editing.
>
> **Authority order:**
> 1. this master execution prompt;
> 2. `docs/handoffs/zero-debt-independent-audit-consolidation-2026-08-09.md`;
> 3. `docs/handoffs/selective-rust-acceleration-follow-up-2026-08-09.md`;
> 4. the original plan and original audit prompt as historical evidence only.
>
> **Directive:** EXECUTE. Produce repaired source, tests and evidence. Do not return another plan-only review.

---

You are the Severian builder responsible for completing the zero-debt embedding/performance programme and the approved selective Rust acceleration work.

Assume prior “READY” and support-tier claims are wrong until reproduced. The independent audit found the core Python repairs substantially sound but found the embedding evidence invalid. Sahil has since approved selective Rust production work because the top-k spike produced a 67.6% end-to-end improvement. Your job is to fix the benchmark/evidence defects, run the complete cross-family embedding bake-off with Qwen3 Embedding 0.6B prioritised, productionise the proven Rust vector seam, re-profile the remaining Rust candidates and return one honest evidence package.

Do not merely critique these instructions. Execute them in the isolated candidate using RED → GREEN → REFACTOR.

## 1. Repository and state — verify first

- Repo/worktree: `/home/kensei/repos/severian-zero-debt`
- Branch: `zero-debt-embedding-perf`
- Pre-handoff-package HEAD: `27b140127d0db8f928c8d7a9ee7d961b3bcf7925`
- Code-final ancestor before generated benchmark/docs changes: `83cb95c`
- Original programme baseline: `c4d2b602e82e48b70cb4299cb9511d0e7a6c064c`
- Production store `/home/kensei/.hermes/severian`: read-only and out of scope
- Canonical checkout `/home/kensei/repos/Severian`: read-only and out of scope

Run:

```bash
cd /home/kensei/repos/severian-zero-debt

git branch --show-current
git rev-parse HEAD
git merge-base --is-ancestor 83cb95c HEAD && echo 'code ancestry OK'
git merge-base --is-ancestor c4d2b602e82e48b70cb4299cb9511d0e7a6c064c HEAD && echo 'baseline ancestry OK'
git diff --name-only 83cb95c..HEAD
git diff --check c4d2b602e82e48b70cb4299cb9511d0e7a6c064c..HEAD
git status --short --untracked-files=all
```

Expected before implementation:

- branch is `zero-debt-embedding-perf`;
- HEAD is a docs-only descendant of `27b140127d0db8f928c8d7a9ee7d961b3bcf7925` and a descendant of `83cb95c`;
- `83cb95c..HEAD` touches only `docs/`;
- whitespace gate exits 0;
- the working tree should be clean. If this package was copied before its docs-only commit, only the four named `docs/handoffs/` files may be untracked. Any other dirty path is a blocker: preserve it, identify its owner and stop rather than cleaning, stashing or overwriting it.

Record the exact live state. Do not replace it with the expected values if it differs.

## 2. Read these files in order

### Current authority

1. `/home/kensei/repos/severian-zero-debt/docs/handoffs/zero-debt-independent-audit-consolidation-2026-08-09.md`
   - Authoritative audit findings, evidence limits and P0/P1 actions.
2. `/home/kensei/repos/severian-zero-debt/docs/handoffs/selective-rust-acceleration-follow-up-2026-08-09.md`
   - Authoritative Rust architecture, candidate ladder, production gates and return format.

### Historical context — do not treat old verdicts as current authority

3. `/home/kensei/repos/severian-zero-debt/docs/handoffs/zero-debt-embedding-audit-prompt-2026-08-09.md`
   - Original audit prompt; some probes and conclusions were found invalid.
4. `/home/kensei/repos/Severian/.hermes/plans/2026-08-08_152340-severian-zero-debt-embedding-performance-rust-spike.md`
   - Original executed programme and spike detail. Its “do not productionise Rust” decision is superseded by Sahil's 09/08/2026 decision.
5. `/tmp/severian-rust-topk-spike/README.md`
6. `/tmp/severian-rust-topk-spike/src/lib.rs`
7. `/tmp/severian-rust-topk-spike/tests/test_parity.py`
8. `/tmp/severian-rust-topk-spike/benchmark-completion.json`
9. `/tmp/severian-rust-topk-spike/benchmark-e2e.json`

`/tmp` is ephemeral. If a spike file has disappeared, record that as a GAP and continue from the durable source/audit; do not fabricate or silently regenerate historical evidence.

## 3. Locked owner decisions

Do not reopen these:

1. Python remains Severian's product/control plane. No full Rust or Go rewrite.
2. The existing Granite-only embedding decision is invalid.
3. Qwen3 Embedding 0.6B is the priority challenger, not a preselected winner.
4. The repaired bake-off must fairly test models outside Granite.
5. The measured 67.6% vector-path end-to-end improvement is significant enough to proceed with production Rust work.
6. Rust must maximise every worthwhile candidate, but each seam needs its own profile, parity and end-to-end evidence.
7. The first Rust seam is packed-vector decode + exact scoring + deterministic top-k as one unit.
8. MinHash/LSH, dense candidate generation and SQLite/native strategies are re-profiled after the vector seam; do not port them blindly.
9. Pure Python remains installable and tested. Use an optional PyO3/maturin companion wheel rather than replacing the root Hatchling backend.
10. No model activation, vector rebuild, merge, push, production-store write, Hermes configuration change or gateway/service change is authorised.
11. Preserve historical evidence. Mark it superseded/invalid where necessary; do not delete or restamp it.

## 4. Working rules

- Work only in `/home/kensei/repos/severian-zero-debt` and owned `/tmp` roots.
- Never read raw private session content into chat or committed logs.
- Generate the approved redacted corpus under a process-owned temporary directory.
- Run the scrubber before writing, scan after writing and bind the detector report to the actual corpus hash.
- Use failing tests before behavioural fixes.
- Preserve direct command exit codes; no `pytest | tail`, masked pipelines or fabricated counts.
- Run collection and execution separately.
- Keep commits small and grouped by benchmark integrity, Rust vector integration, model evidence and docs.
- Do not refactor benchmarked code while a measurement window is active. Finish source changes, freeze the candidate SHA, then run the final bake-off/evidence.
- A builder does not own the final READY verdict. Return `READY FOR INDEPENDENT REVIEW`, `PARTIAL` or `BLOCKED`.

## 5. Execution order

### Phase A — Baseline and RED witnesses

1. Record candidate SHA/status/runtime/hardware.
2. Re-run the existing focused/full gates with a safe local PostgreSQL DSN supplied through `SEVERIAN_PG_DSN`. Do not print credentials.
3. Preserve RED witnesses for every audit defect:
   - corpus SHA hashes the manifest;
   - throughput probe rows contaminate quality scoring;
   - bake-off bypasses the release gate and prints unconditional `ok`;
   - pseudo query kinds use near-copy prefixes;
   - model revisions are not pinned/passed to loaders;
   - process-lifetime RSS makes sequential lane deltas invalid;
   - subset reruns overwrite/mark completed lanes skipped;
   - support-tier hash latency is mislabelled.

Expected historical baseline from the independent audit—not a substitute for fresh results:

- full suite: 564 passed / 11 skipped;
- contracts: 99 passed;
- atomic PG rebuild: 3 passed;
- collection: 575;
- mypy: 60 source files clean;
- Ruff clean;
- pip-audit: no known vulnerabilities;
- privacy tests: 10 passed.

### Phase B — Repair benchmark integrity before generating evidence

Implement all P0/P1 audit repairs:

1. Pass the actual corpus path to `EmbeddingBakeoffRunner`; record separate manifest and corpus hashes.
2. Add a CLI integration test proving each digest binds the intended file.
3. Run encode throughput in a separate fresh adapter/index or only after quality scoring.
4. Assert the quality index contains exactly the manifest corpus IDs before scoring.
5. Make release-gate evaluation mandatory for every completed lane.
6. Persist `gate_passed` and exact `gate_failures`; refuse tier publication when no candidate passes.
7. Remove unconditional `ok` status.
8. Replace near-copy label rotation with meaningful, reviewed query/evidence pairs:
   - real paraphrases;
   - correction/supersession pairs;
   - past-versus-current temporal queries;
   - genuine negative/abstention queries;
   - code/task retrieval;
   - actual multilingual/cross-language pairs;
   - long-context buckets.
9. Store expected document IDs directly; never rematch expected evidence by content.
10. Resolve immutable Hugging Face revisions before loading and pass `revision=` to the runtime.
11. Separate query and document encoding contracts; Qwen instructions apply to queries only. Include a no-instruction control.
12. Run each neural lane in a fresh process and measure true process peak RSS, cold load and artefact bytes.
13. Merge lane checkpoints atomically; reject incompatible corpus/manifest/SHA/revision/runtime contracts and never overwrite a complete run with a subset.
14. Mark the existing 2026-08-09 embedding packet and support tiers `SUPERSEDED — INVALID FOR MODEL SELECTION`; preserve their historical numbers.

Do not begin the final model bake-off yet. First complete Phase C source integration and freeze one candidate SHA.

### Phase C — Productionise the proven Rust vector seam

Execute the full contract in:

`docs/handoffs/selective-rust-acceleration-follow-up-2026-08-09.md`

Required shape:

- optional `severian-native` companion package built with maturin;
- `severian` root remains a pure-Python Hatchling package;
- Python 3.11/3.12-independent canonical scoring contract;
- no unsafe code or panic-reliant input path;
- strict malformed/dimension/finite checks;
- exact `(score DESC, content ASC, memory_id ASC)` ordering;
- explicit `auto|python|rust` backend selection;
- explicit Rust mode fails closed;
- auto fallback is visible in diagnostics/telemetry;
- Rust owns no scope, SQLite connection, model, correction, provenance or canonical-write logic in this slice;
- Python-only and native-installed full suites both pass;
- clean wheels for the supported Linux/Python matrix;
- trusted end-to-end gate replaces the retired standalone 3× gate.

The production gate is at least 25% end-to-end p95 improvement at representative 10K scopes, with no correctness or small-scope regression. The target is to reproduce or improve upon the historical 67.6%, but do not fake an exact match.

### Phase D — Freeze source and run the complete cross-family bake-off

After all source changes in Phases B/C are complete:

1. Commit code/tests.
2. Freeze and record that exact code SHA.
3. Do not refactor benchmarked paths during the run.
4. Force the Python vector backend for model-quality comparison so every model receives the same retrieval implementation.
5. Run at least three fresh-process repetitions per required lane.

Required model matrix:

| Role | Model |
|---|---|
| Deterministic fallback/control | HashEmbedding |
| Existing control | Granite 107M multilingual |
| Granite default challenger | Granite 97M R2 multilingual |
| **Priority challenger** | **Qwen3 Embedding 0.6B** |
| External-family challenger | EmbeddingGemma 300M |
| Lite external-family challenger | Bekko a8m |
| High Granite control | Granite 311M R2 multilingual |
| English-only control | Granite Small English R2 |

Qwen minimum controls:

- query-only instruction and no-instruction;
- 1024, 512 and 384 dimensions where supported;
- multilingual and true cross-language probes;
- CPU p50/p95/p99, cold load, true peak RSS and artefact bytes;
- exact revision, runtime and licence;
- all release-gate metrics.

Apply the 1–4 GB artefact/RAM budget and up-to-2-second interactive CPU query target honestly. No model wins solely from public MTEB or Sahil's prior expectation. If no model passes every Severian gate, return “no selection” and keep the current production default unchanged.

### Phase E — Measure Rust against the selected model and maximise candidates

1. Repeat selected-model end-to-end search with forced Python versus forced Rust at the chosen dimensions.
2. Record IDs/order parity and absolute latency/RSS.
3. Re-profile current MinHash/LSH, dense candidate/Jaccard and SQLite/FFI costs after the vector seam.
4. Use the promotion rules in the Rust follow-up document.
5. Return one evidence-backed disposition for every candidate: `PROMOTE`, `DEFER` or `REJECT`.
6. Implement additional Rust candidates only if they clear the profile/spike gates. Do not implement a cold path simply to make the Rust change look larger.
7. A custom native persistence/index seam requires a separate architecture review; the previously tested sqlite-vec path remains rejected unless its tied-boundary semantics are genuinely fixed.

### Phase F — Final regression, packaging and evidence

Required final gates:

1. Focused RED/GREEN tests for every correction/native seam.
2. Separate full collection and full execution.
3. Python-only full suite.
4. Native-installed full suite.
5. mypy strict, Ruff, compileall and pip-audit.
6. Cargo fmt, clippy with warnings denied, Rust tests and dependency/licence audit.
7. SQLite and live-PostgreSQL contracts.
8. CLI, SDK, API, MCP and Hermes integration regressions.
9. Benchmark gate on every completed embedding lane.
10. Privacy detector bound to the real corpus SHA.
11. Base and native wheels installed in clean Python 3.11 and 3.12 environments.
12. `git diff --check`, secret scan including untracked files and post-test residue review.
13. Fresh independent architecture, QA and security review against the exact final candidate.

## 6. Evidence package

Create a new durable package; do not overwrite the invalid historical packet:

```text
docs/benchmarks/evidence/<DATE>-zero-debt-remediation/
├── README.md
├── fingerprint.json
├── embedding-result.json
├── rust-result.json
├── candidate-dispositions.json
└── privacy-proof.json
```

Every JSON must record or reference:

- producing code SHA and branch;
- manifest hash and actual corpus hash separately;
- runner/script hash;
- immutable model revisions;
- exact command/arguments, seeds and repetitions;
- runtime/dependency versions;
- hardware fingerprint;
- requested, completed, failed and skipped lanes;
- release-gate values/failures;
- raw result hashes;
- Python/Rust backend identity;
- honest limitations.

Raw private corpus, raw session content, credentials and large transient logs must not be committed.

## 7. Commit sequence

Keep at least these boundaries:

1. `test: reproduce embedding evidence-integrity defects`
2. `fix: make embedding benchmark evidence trustworthy`
3. `test: lock cross-version native vector parity`
4. `feat: add optional Rust vector acceleration`
5. `test: verify Python and native package contracts`
6. `docs: publish replacement embedding and Rust evidence`
7. `docs: supersede invalid embedding tiers`

Do not combine source implementation and generated evidence in one commit when the evidence must name the source SHA. Commit source first, then generate evidence against that immutable commit, then commit evidence separately.

## 8. Required return package

Return exactly:

```text
VERDICT
- Python/core candidate: READY FOR INDEPENDENT REVIEW | PARTIAL | BLOCKED
- Embedding selection: selected model/tier OR no valid selection
- Rust vector seam: READY FOR INDEPENDENT REVIEW | BLOCKED
- Additional Rust candidates: PROMOTE | DEFER | REJECT per candidate
- Explicit: no merge, push or activation performed

CANDIDATE IDENTITY
- Base/code/evidence SHAs
- Branch/worktree/status
- Changed paths

AUDIT DEFECTS A–H
- One row per defect: RED witness, fix, GREEN evidence

EMBEDDING RESULTS
- Full matrix including Qwen, Gemma and Bekko
- Revisions/dimensions/instruction contracts/licences
- Quality/gates/latency/RSS/storage
- Selection rationale and rejected/skipped lanes

RUST RESULTS
- Python 3.11/3.12 exact parity
- Kernel and end-to-end latency
- FFI/RSS/memory/error behaviour
- Backend selection/fallback proof
- Base/native wheel evidence
- Disposition for every Rust candidate

TEST/QUALITY
- Collection and execution with direct exit codes
- Python/Rust static/security gates
- SQLite/PostgreSQL/integration lanes
- Independent review verdicts

PRIVACY/PROVENANCE
- Actual corpus hash and detector proof
- Manifest/runner/model hashes
- No raw private data committed

SAFETY
- Production and canonical stores/checkouts untouched
- No merge/push/activation/vector rebuild
- Temporary artefacts accounted for, not deleted without ownership/reference proof

OPEN BLOCKERS
- Every failed/skipped/unavailable lane
```

The builder may say `READY FOR INDEPENDENT REVIEW`; it may not self-declare the programme READY or activate a model/native default. Independent review and Sahil own those decisions.
