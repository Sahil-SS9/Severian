# Candidate ladder — final dispositions (R5, 2026-08-09)

Every row has one disposition with fresh evidence. "Not implemented" is
never used as a disposition. Full numbers: docs/benchmarks/r4-dispositions-2026-08-09.md
and /tmp/rem-r4-profile.json.

| Candidate | Disposition | Fresh evidence |
|-----------|-------------|----------------|
| Packed-vector decode + exact score + deterministic top-k (vector seam) | PROMOTE (DONE) | severian-native productionised; 49/49 bit-exact parity; 2.79-2.94x kernel speedup; 62-94% e2e p95 improvement at 10K scopes; full suites 667/610 with/without native; cargo audit 0 vulns |
| MinHash/LSH signature generation | DEFER — not currently material | 0.4-47ms candidate phase at 1K-10K; <=12.7% of the phase at 10K; R2.2 repairs hold recall while cost stays sub-15% of e2e; spike only if a future profile shows >= 15% |
| Dense candidate/Jaccard | DEFER — not currently material | 0.6-1.8us/pair; bounded deterministic batches preserved; per-pair FFI explicitly prohibited and not warranted at this cost |
| SQLite/native indexing | REJECT — unchanged | sqlite-vec v0.1.9 tied-boundary determinism unproven; ANN not a substitute for the exact default contract; custom native storage seam needs separate design review |
| Tokenisation/content-digest | DEFER — not currently material | 0.1-0.8% of candidate phase; policy-heavy, kept in Python by design |
| FFI conversion/copy overhead | DEFER — amortised | fused decode+score avoids per-row Vec<f64>; e2e traced bytes drop 144MB -> 18.2MB |
