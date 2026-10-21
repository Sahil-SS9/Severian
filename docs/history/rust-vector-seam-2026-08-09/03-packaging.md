# Packaging evidence — Rust vector seam (2026-08-09)

## Wheel identities

- Base: severian-0.3.0-py3-none-any.whl (pure-Python, Hatchling)
- Native: severian_native-0.1.0-cp311-cp311-manylinux_2_34_x86_64.whl
- Native: severian_native-0.1.0-cp312-cp312-manylinux_2_34_x86_64.whl
- Native module: severian_native, version string `severian-native-0.1.0-pyo3-0.29.2`
  (version derived at build time from Cargo.lock via native/build.rs — truthful)

## Clean-venv install results

Python 3.11 (uv venv /tmp/rem-venv-py311, no pip shipped — uv pip used):
- base wheel only: import OK, LocalVectorIndex works, backend_diagnostics()['selected'] == 'python'
- + native wheel: import OK, native_version() OK, real search returns correct hit,
  backend_diagnostics()['selected'] == 'rust'

Python 3.12 (canonical venv):
- full suite native-installed: 667 passed / 11 skipped
- full suite python-only: 610 passed / 13 skipped
- native verification (parity + integration + fallback + score contract): 85/85

## Build tooling

- maturin 1.14.1 (release profile, opt-level 3)
- cargo fmt --check: clean; clippy -D warnings: clean; cargo audit: 0 vulnerabilities
- Licence inventory: all Apache-2.0 OR MIT (pyo3 0.29.2 family) — no copyleft

## Unsupported platforms (stated honestly)

- Only Linux x86_64 verified (AMD Ryzen 9 5900X, CPython 3.11 + 3.12).
- No macOS/Windows/aarch64 wheel was built or tested; no claim is made.
- target-cpu=x86-64 pinned in native/.cargo/config.toml so fsum summation
  order is stable and bit-identical across x86-64 machines.
