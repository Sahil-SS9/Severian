# Benchmark package — 2026-08-05 (current HEAD)

Run at commit `9d7c989` (branch `feat/ai-memory-concepts`) on
2026-08-05T20:02:40Z.

## Gate result

**gate_passed: True** — no gate failures.

| Lane | recall@10 | stale_fact_rate | cross_scope_leakage |
|---|---|---|---|
| severian-sqlite | 1.000 | 0.000 | 0.000 |
| chroma | 1.000 | 0.159 | 0.000 |
| baseline-lexical | 0.933 | 0.174 | 0.000 |

## Lanes included

- `severian-sqlite` — reference backend, live at this HEAD.
- `chroma` — external lane (CVE-2026-45829 known, local-only use).
- `baseline-lexical` — bag-of-words baseline.

## Lanes NOT re-run at this HEAD (honest disclosure)

- `severian-postgres` — requires `--pg-dsn` / `SEVERIAN_PG_DSN`; the DSN
  was not available in the 2026-08-05 release session (redacted per
  credential policy). Last measured evidence: `docs/benchmarks/evidence/2026-08-04/`
  at commit `9749a75`. The manifest used for this run excluded the lane
  (`/tmp/manifest-v1-nopg.json`, providers minus severian-postgres);
  no numbers were fabricated or carried forward as fresh.

Vendor-reported lanes (mem0, hindsight, honcho, supermemory, mempalace,
mnemosyne) are citations only — never run by the harness.

## Files

- `result.json` — gate + per-provider metrics (sha256 2bffe370...)
- `traces.json` — per-query traces (sha256 db5be5fe...)
- `fingerprint.json` — commit, manifest sha256, environment (sha256 ef560fe0...)
