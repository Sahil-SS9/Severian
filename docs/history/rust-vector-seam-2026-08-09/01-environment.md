# Environment fingerprint — Rust vector seam evidence (2026-08-09)

- Date: 2026-08-09
- Git base SHA: cab8088 (docs: add zero-debt remediation and Rust handoff)
- Branch: zero-debt-embedding-perf
- Porcelain: 21 changed/untracked entries (uncommitted work-in-progress, per sign-off rule)
- Python: 3.12.3 (canonical venv, /home/kensei/repos/severian-zero-debt/.venv)
- maturin: 1.14.1 (/home/kensei/.local/bin/maturin)
- Rust: 1.97.1 (8bab26f4f 2026-07-14)
- CPU: AMD Ryzen 9 5900X 12-Core Processor
- OS: Linux 7.0.0-28-generic x86_64
- Platform support: Linux x86_64 only; CPython 3.11 + 3.12 wheels built and verified. No cross-platform claim is made from this evidence.
- Native wheel: severian_native-0.1.0 (pyo3 0.29.2, maturin-built, manylinux_2_34_x86_64)
- cargo audit: 0 vulnerabilities (pyo3 0.29.2 resolves RUSTSEC-2025-0020, RUSTSEC-2026-0176, RUSTSEC-2026-0177)
- Licence inventory: Apache-2.0 OR MIT (pyo3 family, unicode-ident, etc.); no copyleft deps

## Key evidence artifacts (owned temp root /tmp)

- /tmp/severian-rust-topk-spike/benchmark.json — R0.3 kernel + e2e lanes (dedicated per-run file; never shared benchmark.log)
- /tmp/rem-r0-3-kernel.log — live lane log for the same run
- /tmp/rem-native-gate-10k-dim{384,512,768,1024}.json — R3 e2e perf gate, python vs rust
- /tmp/rem-native-gate-10k-dim768-pyo3-0.29.json — repeat run on pyo3-0.29.2 wheel (93% p95 improvement at dim 768)
- /tmp/rem-r4-profile.json — R4 dreaming-candidate re-profile
- /tmp/severian-corpus-1938368-rem-rebuilt/corpus.json — rebuilt embedding corpus (5,000 obs, sha d463453e642ece0f; query-gen defect fixed 2026-08-09, see 05-corpus-query-defect.md)
- /tmp/rem-phase-d-bakeoff/ — Phase D bake-off rep-1 output (checkpointed lane-*.json + embedding-bakeoff.json)
