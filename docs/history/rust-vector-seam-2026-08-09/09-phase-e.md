# Phase E — Rust vs selected model e2e (2026-08-09)

## Method (master handoff Phase E items 1-2)

- Forced Python vs forced Rust in SEPARATE fresh processes (no same-process
  backend switching — clean memory/latency isolation)
- Dimension: 1024 = qwen-0.6b native dim (best-ranked lane, Phase D)
- 10,000 rows in one scope, content-hash embeddings (real encode path via
  Memory.upsert → embedding.encode → packed storage — same production seam)
- 15 reps after warm, p50/p95/p99, ID/order parity, backend diagnostics
- Script: scripts/benchmark_phase_e.py; evidence: /tmp/rem-phase-e-dim1024.json

## Results

| backend | p50 | p95 | p99 | selected | native version |
|---------|-----|-----|-----|----------|----------------|
| python  | 1206.8ms | 1271.9ms | 1271.9ms | python | (none) |
| rust    | 68.6ms   | 77.7ms   | 77.7ms   | rust   | severian-native-0.1.0-pyo3-0.29.2 |

- p95 improvement: 93.9% (1271.9ms -> 77.7ms)
- ID/order parity: TRUE (bit-identical hit lists and order)
- The rust seam is 16.4x faster at p95 at the selected model's dimension.

## Interpretation

- The vector seam's benefit holds at dim 1024 with the FINAL pyo3-0.29.2
  wheel: 93.9% e2e p95 improvement (consistent with the 93% dim-768 repeat;
  the earlier 64.9% at dim 1024 was measured on the older 0.23 wheel and is
  superseded).
- Parity is exact — no quality trade for speed (bit-identical IDs/order).
- This is the production shape for the qwen-0.6b workload: query encode +
  SQLite fetch + packed decode + exact score + deterministic top-k.
