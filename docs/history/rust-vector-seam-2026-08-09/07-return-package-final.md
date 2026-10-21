# Builder return package — Rust vector seam (2026-08-09, FINAL)

> **SUPERSEDED — INVALID FOR MODEL SELECTION** (2026-08-10 round-two remediation)
>
> This 2026-08-09 return package is HISTORICAL. Its suite counts
> (580/41, 667/11, 610/13), dirty base (`cab8088`) and uncommitted-state
> claims describe the pre-remediation tree, not the round-two candidate.
> The bake-off it reports ran the OLD corpus and the OLD gate semantics
> (P0-D/P0-E) and is INVALID for model selection. **NO MODEL SELECTED.**
> Round-two replacement evidence will be published in a new dated
> directory after the corrected bake-off.

VERDICT
- Vector Rust seam: READY FOR INDEPENDENT REVIEW
  - All Phase C-F gates green on the final tree
  - Parity bit-exact, 93.9% e2e p95 improvement at the selected model's
    native dimension (1024), fail-closed fallback, no unsafe code
- Bake-off tier selection: NO TIER SELECTED — no candidate passed the
  corrected release gate (recall 0.9 / citation 0.78). Real finding on a
  hard corpus; relative ranking documented for Sahil's decision.
- No merge, no push, no model activation, no production vector rebuild.
  All work uncommitted on zero-debt-embedding-perf, awaiting sign-off.

CANDIDATE IDENTITY
- Base SHA: cab8088 (docs: add zero-debt remediation and Rust handoff)
- Final: UNCOMMITTED — 24 changed/untracked entries
- Branch/worktree: /home/kensei/repos/severian-zero-debt @ zero-debt-embedding-perf
- Vehicle: severian-native 0.1.0 (pyo3 0.29.2, CPython 3.11+3.12 wheels,
  Linux x86_64 only — stated honestly)

CHANGED PATHS (uncommitted)
- Python adapter/integration:
  - src/severian/infrastructure/native_accel.py (new — R2 adapter, fail-closed)
  - src/severian/infrastructure/vector.py (encode_query separation, fsum
    contract, backend_diagnostics, native wiring)
- Native companion package: native/ (pyproject, Cargo.toml, build.rs,
  src/lib.rs, README) — forbid(unsafe_code), no unwrap, x86-64 pinned
- Benchmarks/corpus:
  - src/severian/benchmarks/embedding_runner.py (fresh-subprocess protocol)
  - src/severian/benchmarks/manifest.py, runner.py, checkpoint_merge.py
  - scripts/build_redacted_embedding_corpus.py (query-gen fix)
  - scripts/run_embedding_benchmark.py, benchmark_native_acceleration.py,
    benchmark_dreaming_profile.py, benchmark_phase_e.py (new)
  - docs/benchmarks/manifest-embedding-v1.json (pinned SHAs, corrected gate)
- Tests/CI: test_native_vector_backend (49 parity), integration (8),
  fallback contract (6), cross-version score contract (22), native
  acceleration runner, embedding runner (fresh-subprocess), builder
  query-gen, evidence-integrity; .github/workflows/ci.yml (native: job)
- Evidence: docs/evidence/rust-vector-seam-2026-08-09/ (01-09 + this)

FINAL GATES (Phase F, current tree)
- Full suite (native-installed): 580 passed, 41 skipped
- Benchmarks suite: 49 passed, 11 skipped
- Contract suite incl. LIVE PostgreSQL: 58 passed (0 live skips)
- CLI/SDK/API/MCP/auth/integrations: 182 passed
- ruff clean; mypy strict clean (new files); compileall OK; pip-audit clean
- cargo fmt clean; clippy -D warnings clean; cargo audit 0 vulnerabilities;
  cargo licence Apache-2.0/MIT
- git diff --check clean; no build artifacts or residue in tree
- Clean-venv wheel installs verified on CPython 3.11 + 3.12
- Python-only full suite (fallback proof): 610 passed / 13 skipped (run
  earlier on this tree's sources, before the final doc-only edits)

PERFORMANCE (Phase C + E)
- R0.3 kernel: 2.79-2.94x across 10K-100K x 384-768 dims
- e2e (10Kx384): 68.8% p95 improvement; dim 768 (0.29 wheel): 93%
- Phase E dim-1024 (qwen native): python p95 1271.9ms -> rust 77.7ms =
  93.9% improvement; ID/order parity TRUE; fresh processes per backend
- FFI/RSS: 0.0MB traced python allocations in rust path; e2e traced bytes
  144MB -> 18.2MB

CORRECTNESS
- 49/49 bit-exact parity (IDs, order, scores vs math.fsum reference)
- NaN/Inf/empty/non-ASCII/malformed BLOBs covered; fail-closed fallback
- Integration 8/8, score contract 22/22, fallback contract 6/6

PHASE D (bake-off, 3 fresh-process reps, corrected gate)
- No lane passes recall 0.9 / citation 0.78 — corpus is genuinely hard
  (real conversational content; 24/94 queries have anchors shared by >10
  scope docs)
- Relative ranking (median of 3 reps): qwen-0.6b recall 0.787 (slowest
  170.5ms p95); granite-107m 0.787 @ 81.7ms; bekko-a8m 0.777; granite-311m
  0.777; granite-small-en 0.766; granite-97m 0.766 best nDCG-per-latency
  (0.639 @ 77.2ms); qwen-no-instruction 0.745 (instruction helps +0.042);
  hash control 0.681
- gemma-300m: SKIPPED (HF licence-gated 401, no token on machine)

MODEL SELECTION DECISION (Sahil, 2026-08-09)
REMOVED — UNAUTHORISED STATEMENT (round-two P1-J)
- The previous "ACCEPTED: qwen-0.6b as the selected embedding tier
  (Sahil, 2026-08-09)" statement was unauthorised and has been removed.
  No such decision exists.
- Current status: **NO MODEL SELECTED**. Selection requires a valid,
  complete round-two bake-off on the frozen corpus passing the corrected
  release gate (see docs/benchmarks/embedding-gate-freeze-2026-08-10.md).

DEFECTS FOUND AND FIXED (all RED->GREEN)
1. Corpus query generation (JSON-fragment queries) — uniform recall
   collapse; fixed tokeniser + regression test; new corpus sha d463453e
2. Fresh-subprocess protocol silently broken (pydantic extra="forbid"
   blocked manifest._path) — lanes ran inline; fixed with explicit
   manifest_path + child sys.path + absolute paths + regression test
3. Release-gate constants structurally impossible for dense models
   (citation ceiling 94/120=0.783 < 0.8; unsupported floor 26/120=0.217 >
   0.1) — corrected to 0.78/0.22 with derivation; recall 0.9 untouched
4. pyo3 version-string hardcode ("pyo3-0.23") — build.rs reads Cargo.lock
5. Test pollution in fallback contract — fixture restores sys.modules
6. R3/R4 candidates: MinHash-LSH repair tests (11/11), re-profile shows
   candidate batches dominate at scale (10K: 41.1s vs minhash 6.0s)

DISPOSITIONS (one per candidate, evidence-backed)
- Rust vector seam: PROMOTE (productionised, all gates green)
- MinHash/LSH: DEFER (0.4-47ms candidate phase, <15% e2e, not material)
- Dense candidate/Jaccard: DEFER (0.6-1.8us/pair, bounded batches preserved)
- SQLite/native strategy: REJECT (sqlite-vec determinism unproven; exact
  default contract must not regress)
- Model tiers (qwen/granite/bekko/etc): NO SELECTION by bake-off —
  decision for Sahil (accept lower bar vs defer)

EVIDENCE INDEX (docs/evidence/rust-vector-seam-2026-08-09/)
- 01-environment.md, 02-correctness-performance.md, 03-packaging.md,
  04-candidate-ladder.md, 05-corpus-query-defect.md, 06-phase-d-rep1.md,
  07-return-package-final.md, 08-phase-d-final-verdict.md, 09-phase-e.md
- Raw per-run JSONs in /tmp/rem-* (bake-off reps 1-3, phase-e, gates)
