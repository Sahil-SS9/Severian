# Evidence status — HISTORICAL / SUPERSEDED (C-07)

This directory (`2026-08-02/`) contains the ORIGINAL manifest-v1 quality-gate
run. It is **historical and superseded**:

- It reports recall 0.933, citation precision 0.103, stale 0.159 (SQLite) /
  0.145 (PostgreSQL) — pre-fix numbers.
- Its `result.json` has no `git_sha`; its fingerprint records commit
  `27cf071`; git shows the evidence was last touched by `56b5dc9`.
- The current, authoritative manifest-v1 evidence is in
  `../2026-08-04/` (producing SHA 876f616, correction lane
  `fix/auditor-corrections-2026-08-04`): recall 1.000, cit_prec 0.9375
  (honest gate FAIL), stale 0.000.

Per the auditor correction packet (C-07): this directory is preserved
immutably. Do NOT restamp it with a new SHA. Fresh reproduction is
authoritative when it conflicts with this historical record.
