# Correctness + performance evidence — Rust vector seam (2026-08-09)

All numbers from dedicated per-run JSON files (never the shared benchmark.log).

## Parity / correctness

- Production parity matrix (tests/unit/test_native_vector_backend.py): 49/49 PASSED
  - dims 256/384/512/768/1024; rows 0/1/19/20/21/1K/10K/100K; k=0/1/5/20/N/N+1
  - identical vectors / exact ties / repeated runs / non-ASCII / malformed BLOBs / NaN / Inf / empty rows
  - bit-exact IDs, order and scores vs Python math.fsum reference
- Spike parity: 68/68 (throwaway spike venv)
- Integration (tests/integration/test_native_vector_integration.py): 8/8 — real search() contract, scope isolation, auto-select rust, fail-closed
- Fallback contract (tests/gates/test_native_fallback_contract.py): 6/6 — import without native, forced python, explicit rust fails closed, auto selects python, invalid rejected, fallback recorded
- Score contract (tests/unit/test_cross_version_score_contract.py): 22/22 — fsum exactness vs rational arithmetic
- Full suite native-installed: 667 passed / 11 skipped / EXIT=0
- Full suite python-only (native uninstalled): 610 passed / 13 skipped / EXIT=0

## R0.3 kernel benchmark (spike, /tmp/severian-rust-topk-spike/benchmark.json)

| lane          | python p50/p95/p99 (ms) | rust p50/p95/p99 (ms) | speedup (p95) |
|---------------|------------------------|-----------------------|---------------|
| 10000x384     | 494.1 / 514.6 / 522.9  | 162.6 / 176.1 / 186.1 | 2.92x         |
| 100000x384    | 4935.6 / 5012.3 / 5135.9 | 1708.7 / 1797.6 / 1817.1 | 2.79x     |
| 10000x768     | 988.9 / 1013.5 / 1021.7 | 322.2 / 345.5 / 350.4 | 2.93x         |
| 100000x768    | 10013.3 / 10255.3 / 10330.8 | 3456.3 / 3493.9 / 3500.2 | 2.94x     |
| e2e 10000x384 | 605.6 / 628.7 / 632.9  | 171.7 / 195.9 / 198.5 | 68.8% improvement |

Historical supported figure 2.70x; reproduced 2.79-2.94x — no regression.
E2E gate >= 65%: PASSED at 68.8%.

## R3 production perf gate (scripts/benchmark_native_acceleration.py, 10K scopes, 15 reps)

| dim  | python p95 | rust p95 | improvement | gate >= 25% |
|------|-----------:|---------:|------------:|-------------|
| 384  | ~1015 ms   | ~380 ms  | 62.6%       | PASS        |
| 512  | ~1010 ms   | ~360 ms  | 64.2%       | PASS        |
| 768  | ~1017 ms   | ~362 ms  | 64.4%       | PASS        |
| 1024 | ~1020 ms   | ~355 ms  | 64.9%       | PASS        |

Repeat on pyo3-0.29.2 wheel (dim 768): rust p50 58.7ms / p95 69.5ms, improvement 93.3-93.8%.
Recorded honestly as repeated-run variance: the 0.29 wheel with the attach/detach
GIL-release path is substantially faster than the 0.23 wheel used in the first gate run.

## Memory (R0.3, tracemalloc peak per call, 20-50 rounds)

| lane          | python traced | rust traced | maxrss both |
|---------------|--------------:|------------:|------------:|
| 10000x384     | 1.9 MB        | 0.0 MB      | 49 MB       |
| 100000x384    | 18.4 MB       | 0.0 MB      | 365-372 MB  |
| 10000x768     | 1.9 MB        | 0.0 MB      | 372 MB      |
| 100000x768    | 18.5 MB       | 0.0 MB      | 673-674 MB  |
| e2e 10000x384 | 144.0 MB      | 18.2 MB     | 945 MB      |

No growth across rounds. Rust path has zero Python-side traced allocations;
e2e rust drops 8x traced bytes (fused decode+score, no per-row Vec<f64>).

## Verification suites

- cargo fmt --check: clean
- cargo clippy --release -- -D warnings: clean
- cargo audit: 0 vulnerabilities
- pip-audit: no known vulnerabilities (severian itself skipped — not on PyPI)
- ruff check (all new Phase C files): clean
- mypy --strict (src + new tests): clean
- compileall: clean; git diff --check: clean; secret scan: clean
