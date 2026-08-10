# Phase R4 — Candidate dispositions (fresh evidence, 2026-08-09)

Source: /tmp/rem-r4-profile.json (dedicated per-run JSON)
Runner: scripts/benchmark_dreaming_profile.py
Code SHA: dirty-patch identity on zero-debt-embedding-perf (uncommitted; see
  git diff for exact identity). Python 3.12.3, Linux x86_64.

## Promotion rule (R4.2)

A second Rust candidate may proceed only when it consumes >= 15% of the
relevant end-to-end workflow AND a bounded spike projects >= 20% e2e
improvement.

## Measured candidate-phase costs (wall, per corpus)

| corpus                 | n     | phase wall | tokenisation | minhash | candidate batches | jaccard/pair |
|------------------------|------:|-----------:|-------------:|--------:|------------------:|-------------:|
| diverse-1k             | 1000  | 1.4 ms     | 0.3%         | 39.7%   | 60.0%             | 0.80 us      |
| shared-template-1k     | 1000  | 0.8 ms     | 0.4%         | 67.3%   | 32.3%             | 1.80 us      |
| exact-duplicates-1k    | 1000  | 0.4 ms     | 0.8%         | 94.0%   | 5.1%              | 0.60 us      |
| contradiction-band-1k  | 1000  | 0.7 ms     | 0.3%         | 35.6%   | 64.1%             | 1.30 us      |
| stopword-heavy-1k      | 1000  | 0.9 ms     | 0.5%         | 97.6%   | 1.9%              | 1.50 us      |
| large-bounded-10k      | 10000 | 47.2 ms    | 0.1%         | 12.7%   | 87.2%             | 1.60 us      |

## End-to-end anchor

- Vector search e2e (10K x 384, storage read included): python p95
  628.7 ms, rust p95 195.9 ms (R0.3 spike benchmark.json).
- Dreaming candidate phase at the same scale (large-bounded-10k):
  47.2 ms — **7.5% of the python e2e search alone**, and dreaming runs
  vector scoring on top of that. No candidate exceeds 15% of the
  relevant workflow.

## Dispositions

| candidate | disposition | fresh evidence |
|-----------|-------------|----------------|
| MinHash/LSH signature generation | DEFER — not currently material | minhash phase 0.4-47 ms; <= 12.7% of the candidate phase at 10K; the R2.2 repairs (condensation, bounded batches) hold recall while keeping cost sub-15% of e2e. Spike only if a future profile shows >= 15%. |
| Dense candidate/Jaccard | DEFER — not currently material | jaccard 0.6-1.8 us/pair; bounded deterministic batches preserved; crossing FFI per pair is explicitly prohibited by R4.3 and not warranted at this cost. |
| SQLite/native indexing | REJECT — unchanged | sqlite-vec v0.1.9 remains rejected (tied-boundary determinism unproven); ANN is not a substitute for the exact default contract; a custom native storage seam requires a separate design review. |
| Packed-vector decode + score + top-k | PROMOTE — DONE (R0.3/R1-R3) | productionised as severian-native; 49/49 bit-exact parity; 2.79-2.94x kernel speedup; 62-94% e2e p95 improvement at 10K; full suite 667/610 passed with/without native. |
| Tokenisation/content-digest | DEFER — not currently material | 0.1-0.8% of candidate phase; policy-heavy (kept in Python by design). |
| FFI conversion/copy overhead | DEFER — amortised | fused decode+score avoids per-row Vec<f64>; e2e traced bytes drop 144 MB -> 18.2 MB (R0.3). |

Every row has a disposition with fresh numbers; "not implemented" is not
used as a disposition.
