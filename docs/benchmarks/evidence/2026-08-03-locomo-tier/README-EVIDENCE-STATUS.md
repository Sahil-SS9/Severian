# Evidence status — HISTORICAL relative to current main (C-07)

This directory (`2026-08-03-locomo-tier/`) contains the LOCOMO-tier run
produced at commit `75b61fc`. It is **historical relative to current main**:

- fingerprint.json records producing commit `75b61fc74196a9edac003d36e1abf382256fd0be`.
- It predates the C-08 deterministic-tiebreak fix and the C-01 Chroma
  isolation fix. Its MRR (0.792-0.801) reflected random-UUID tie instability;
  the current deterministic run (see `../2026-08-04/`) measures MRR 0.885
  with byte-identical repetition.
- The authoritative LOCOMO evidence is `../2026-08-04/locomo-*.json`
  (producing SHA 876f616): recall 0.923, MRR 0.885, temporal 1.000,
  stale 0.000.

Preserved immutably per the auditor correction packet (C-07). Do not
restamp. Fresh reproduction is authoritative when it conflicts.
