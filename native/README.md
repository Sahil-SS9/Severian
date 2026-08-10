# severian-native

Optional native vector-search acceleration for Severian — exact top-k,
bit-identical to the pure-Python `math.fsum` reference.

## What this is

A small PyO3 companion package that accelerates
`LocalVectorIndex.search()`'s exact bounded top-k stage. It is
OPTIONAL: the base `severian` package works fully without it
(pure-Python fallback).

- distribution: `severian-native`
- module: `severian_native`
- exported: `exact_top_k(query, rows, k)`, `native_version()`

## Score contract

`dot(query, row)` computed in f64 after f32 little-endian BLOB decode,
summed with Shewchuk compensated summation — bit-identical to CPython
`math.fsum`, the locked cross-version score contract (R0.2). Total
tiebreak order: score DESC, content ASC, memory_id ASC. No tolerance
that can change ranking.

## Selecting the backend

`SEVERIAN_VECTOR_BACKEND=auto|python|rust` (or the constructor option):

- `auto` (default): Rust when the wheel is installed, else Python;
- `python`: always the pure-Python reference path;
- `rust`: fail closed — raises a clear configuration error when the
  native module is absent, and never silently falls back on native
  failure.

The selected backend is visible through
`LocalVectorIndex.backend_diagnostics()` (requested/selected/native
version/fallback reason/call and failure counters). Backend identity is
diagnostics only — it never alters vectors or ranking.

## Building

```sh
cd native
maturin build --release   # or: maturin develop
```

Requires a Rust toolchain and maturin. The base package remains a
pure-Python Hatchling build; this companion is built independently.

## Platform support

Verified on Linux x86_64 for CPython 3.11 and 3.12 only. Do not claim
cross-platform support from Linux-only evidence. Parity flags are
pinned to `target-cpu=x86-64` (see `.cargo/config.toml`) so FP
summation order is stable and bit-identical across machines.

## Verifying parity

```sh
# from the repo root, with the wheel installed:
pytest tests/unit/test_native_vector_backend.py      # parity matrix
pytest tests/integration/test_native_vector_integration.py
pytest tests/gates/test_native_fallback_contract.py
```
