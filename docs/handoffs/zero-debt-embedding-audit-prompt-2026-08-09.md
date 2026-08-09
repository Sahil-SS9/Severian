# SEVERIAN ZERO-DEBT EMBEDDING PERFORMANCE — INDEPENDENT AUDIT PROMPT (2026-08-09)

> Paste everything below the line into a FRESH chat with an independent
> auditor/reviewer agent. Self-contained: no prior context needed.
> Producing baseline: code final `83cb95c`; HEAD `27b1401` adds docs only.

---

You are an independent auditor reviewing the Severian "zero-debt embedding
performance" plan on branch `zero-debt-embedding-perf`. Assume every claim
below is WRONG until you have reproduced it yourself. Never trust the
repo's own docs — verify against source and live execution. Do NOT modify
source; read-only audit. A failed command IS the finding. No fabricated
numbers. Your deliverable MUST include a recommended next-steps/actions
section for the owner.

## Repository state (verify FIRST)

- Repo: `/home/kensei/repos/severian-zero-debt`
- Branch: `zero-debt-embedding-perf`
- HEAD: ANY descendant of `83cb95c` whose diff `83cb95c..HEAD` touches
  ONLY `docs/` (incl. evidence JSON under `docs/benchmarks/evidence/`) —
  if HEAD touches code, that is a finding
- Code/evidence producing SHA: `c92769c` (all scale + bake-off evidence)
- Code final (includes 2 remediation commits): `83cb95c`
- Original plan baseline: `c4d2b60`

```bash
cd /home/kensei/repos/severian-zero-debt
git status --short          # expect: clean EXCEPT ?? docs/handoffs/ (this prompt file)
git branch --show-current   # expect: zero-debt-embedding-perf
git rev-parse HEAD          # any descendant of 83cb95c
git merge-base --is-ancestor 83cb95c HEAD && echo "code-final ancestry OK"
git diff --name-only 83cb95c..HEAD | cat
                            # must be docs/ ONLY — any code change is a finding
git merge-base --is-ancestor c4d2b60 HEAD && echo "baseline ancestry OK"
git diff --check c4d2b60..HEAD   # expect: exit 0 (no whitespace errors)
```

If ANY of the above fails, stop and report — do not continue.

## What the plan was

11 performance/correctness commits on top of `c4d2b60`, plus 2 remediation
commits and 1 docs commit (15 total on the branch):

- exact bounded top-k, packed float32 BLOB vector storage
- embedding-space identity/stamping (fingerprint, no silent fallback)
- atomic side-by-side vector rebuild + rollback (SQLite AND PostgreSQL)
- incremental outbox projection, batch ingest, chunked ack
- linear pending-observation queries, dense dreaming/LSH repair
- redacted real-data embedding corpus builder + reproducible bake-off

Rust top-k spike (separate, throwaway at `/tmp/severian-rust-topk-spike/`):
INVALIDATED for production — kernel speedup 2.70–2.91x < 3.0x gate;
e2e 67.6% improvement MET; bit-exact parity 68/68 MET; NO production
integration. Verify the absence claim:
`grep -rn "topk\|top_k" src/severian/` → expect ZERO matches (the in-repo
search top-k is pure-Python heapq, `vector.py:357-369`); `grep -rn "pyo3" src/`
→ expect ZERO matches (no Rust extension imported).

## What was corrected (2 remediation commits, after internal review)

1. `pgvector_index.py`: scope index name was hardcoded
   `ix_memory_vectors_scope` in BOTH the SQLAlchemy `Index(...)` and the
   raw `CREATE INDEX` — any side-by-side rebuild collided with the live
   table's index (`DuplicateTable`); every PG rebuild failed before this.
   Now `ix_{table_name}_scope` (live table keeps canonical name).
2. `rebuild.py` `_nonce()` used `-`, producing invalid unquoted PG
   identifiers; now `_` separator.
3. `rebuild.py` swap: renamed the candidate meta to the wrong name
   (`vector_space_meta` is alembic-only; live meta is
   `memory_vectors_meta`), never moved indexes alongside renamed tables
   (PG table renames do NOT rename indexes), and the
   `keep_rollback=False` path dropped the NEW live meta. All fixed.
4. `tests/unit/test_atomic_rebuild_pg.py` added (3 tests, live-PG
   skip-aware) — the internal reviewer's #3 finding was "no test
   exercises `rebuild_pg_vectors`"; the tests immediately exposed the
   three defects above.

## Claims to verify

### A. Full regression suite (MUST run, ~2.5 min)

```bash
cd /home/kensei/repos/severian-zero-debt
: "${SEVERIAN_PG_DSN:?Set SEVERIAN_PG_DSN to the dedicated test database; do not put credentials in this document}"
.venv/bin/python -m pytest -q -p no:cacheprovider
```

- EXPECTED (with PG DSN): `564 passed, 11 skipped, 1 warning`, exit 0.
  Without PG DSN: PG-live tests skip (expect ~49+ skips). Either is fine —
  note which you ran.
- Evidence: this exact run produced those counts at HEAD `27b1401` on
  2026-08-09.

### B. Contract lanes, live PostgreSQL (MUST run, ~10 s)

```bash
: "${SEVERIAN_PG_DSN:?Set SEVERIAN_PG_DSN to the dedicated test database; do not put credentials in this document}"
.venv/bin/python -m pytest tests/contract/ -q -p no:cacheprovider
```

- EXPECTED: `99 passed, 1 warning`. These include live PG 16.14 lanes.

### C. PG rebuild contract — the remediation (MUST run, ~2 s)

```bash
: "${SEVERIAN_PG_DSN:?Set SEVERIAN_PG_DSN to the dedicated test database; do not put credentials in this document}"
.venv/bin/python -m pytest tests/unit/test_atomic_rebuild_pg.py -q -p no:cacheprovider
```

- EXPECTED: `3 passed`. Covers: swap+verify+rollback presence,
  no-keep-rollback, failure-leaves-original (no candidate/prev litter).
- Then inspect the three fixes in source (grep the identifiers above)
  and confirm the test actually asserts the right things (read it).

### D. Static gates (MUST run, ~2 min)

```bash
.venv/bin/python -m pytest --collect-only -q -p no:cacheprovider   # expect: 575 tests
.venv/bin/mypy src/severian            # expect: Success: no issues found in 60 source files
.venv/bin/ruff check src tests scripts # expect: All checks passed!
.venv/bin/pip-audit                    # expect: no known vulnerabilities
                                       # (severian itself skips: not on PyPI)
```

### E. Privacy/redaction proof (MUST run, ~1 min)

```bash
.venv/bin/python -m pytest tests/cli/test_build_redacted_embedding_corpus.py -q -p no:cacheprovider
```

- EXPECTED: `10 passed` — asserts redaction of emails/phones/credentials
  BEFORE write and post-redaction secret/PII detection.
- Then scan any remaining corpora for leaked PII (the exact R2 corpus
  file, sha `de09ea4e482de39b8946430fec14dcc8b5d7e67839d85da2d31e0203f5904839`,
  was produced under /tmp and is ephemeral — it may be gone; if so record
  the exact-corpus rescan as a GAP, the durable proof is the committed
  builder + tests + the sha in the packet):

```bash
for f in /tmp/severian-corpus-*/corpus.json; do
  python3 -c "
import json, re
d = json.load(open('$f'))
content = ' '.join(str(r).lower() for r in d.get('observations', d.get('records', []))[:200])
emails = len(re.findall(r'[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}', content))
phones = len(re.findall(r'[0-9]{3}[-.][0-9]{4}', content))
print('$f', emails, 'emails', phones, 'phones')
"
done
```

- EXPECTED: `0 emails 0 phones` per corpus (author spot-checked 5 of 242
  remaining corpora: all 0/0).

### F. Evidence fidelity — committed packet vs source runs (MUST run, ~1 min)

Committed packet: `docs/benchmarks/evidence/2026-08-09-zero-debt/`
(`fingerprint.json`, `result.json`, `README.md`).
Source-of-truth run files (may still exist in /tmp, ephemeral):
`/tmp/zero-debt-scale-p7/scale.json` (1K/10K),
`/tmp/zero-debt-scale-v5/scale.json` (100K),
`/tmp/zero-debt-embedding-r2/embedding-bakeoff.json` (embedding lanes).

```bash
python3 -m json.tool docs/benchmarks/evidence/2026-08-09-zero-debt/fingerprint.json > /dev/null && echo valid
python3 -m json.tool docs/benchmarks/evidence/2026-08-09-zero-debt/result.json > /dev/null && echo valid
```

- EXPECTED: every number in the packet matches the source JSONs
  VERBATIM (docs/s, p50/p95, recall@10, MRR, nDCG@10, dims, RSS, warmup
  flags, corpus sha, git sha). Spot-check at least: 1K/10K sqlite+PG
  docs/s + p95; 100K docs/s; granite-311m + granite-small-en recall@10 /
  MRR / nDCG@10 / p95; warmup flags.
- The packet's README must honestly state: 100K lane is PARTIAL warmup on
  the candidate SHA (indicative, not the final 100K claim); the only
  complete-warmup 100K packet is on an OLDER SHA (`e6ead47`) and is trend
  context only.
- Also verify the support tiers doc (`docs/benchmarks/support-tiers-2026-08-09.md`)
  selections match the packet numbers and that no doc claims a
  complete-warmup 100K on the candidate SHA.

### G. Wheel / packaging (MUST run, ~3 min)

```bash
cd /home/kensei/repos/severian-zero-debt
rm -rf dist build
uv build --python 3.11            # expect: exit 0, wheel ~186KB
uv venv /tmp/auditor-wheel --python 3.11
uv pip install --python /tmp/auditor-wheel/bin/python dist/severian-0.3.0-py3-none-any.whl
/tmp/auditor-wheel/bin/python -c "import severian; print(severian.__file__)"  # expect: site-packages path
```

- EXPECTED: build clean, fresh-venv import OK. The author rebuilt the
  wheel at the final SHA before dispatch (dist/ present, 186115 bytes).

### H. Scale + embedding re-run (TIME-BOXED — see table)

Optional if you have time budget: re-run the 1K/10K scale lane
(`scripts/` scale runner with same seed) and compare docs/s within
run-to-run jitter (author saw 245.8/243.7 sqlite, 151.0/148.5 PG).
Do NOT re-run the 100K lane (hours) or re-download embedding models
(GBs) — verify provenance instead. A declared skip is a documented
finding, not an omission.

## Honest limitations (documented, not hidden — verify they exist)

1. 100K lane on candidate SHA is PARTIAL warmup (35.5K/47.5K warmed);
   a complete-warmup 100K run on `c92769c` is outstanding. The plan's
   "100K supported" claim is NOT yet fully evidenced on this SHA.
2. `benchmark.log` in the spike dir is NOT trustworthy (two-writer
   interleave from orphaned processes); authoritative evidence is the
   per-run JSON files. No production code depends on it.
3. `granite-small-en` `peak_rss_mb: 2.8` in the packet is anomalous
   (likely a measurement artifact) — copied verbatim, flagged in the
   packet, not used for any claim.
4. Scale/bake-off evidence was produced at `c92769c`; the 2 remediation
   commits touch the PG rebuild path (correctness only, not the measured
   hot paths). The committed packet records this provenance.
5. Rust spike lives in `/tmp` (throwaway, evidence-only) and is not part
   of the production candidate.
6. Single local wheel — no cross-platform claim.
7. Source-of-truth run JSONs live in /tmp (ephemeral); the committed
   packet under `docs/benchmarks/evidence/` is the durable record.

## Time-box guidance

| Lane | Time | Action |
|---|---|---|
| State verification | ~2 min | MUST run |
| A. Full regression | ~2.5 min | MUST run |
| B. Contract lanes | ~10 s | MUST run |
| C. PG rebuild contract | ~2 s | MUST run |
| D. Static gates | ~2 min | MUST run |
| E. Privacy proof | ~1 min | MUST run |
| F. Evidence fidelity | ~1 min | MUST run |
| G. Wheel | ~3 min | MUST run |
| H. 1K/10K re-run | ~10 min | OPTIONAL — jitter comparison |
| H. 100K re-run | hours | SKIP — provenance only |
| Model downloads | GBs | SKIP — provenance only |

## Deliverable format

```
VERDICT: READY | NEEDS CORRECTION | NOT READY   (one line)

VERIFIED:
  - [claim] reproduced: command -> output summary

REFUTED / QUESTIONABLE:
  - [claim] expected X, got Y (command, output)

GAPS:
  - [claim] not verified because <reason> (time/env/credentials)

RECOMMENDED NEXT STEPS / ACTIONS:
  - What the owner should do next, prioritised (e.g. complete-warmup 100K
    run, merge/publish decision, CI wiring for PG rebuild contract, model
    revision pinning, cross-platform wheel, docs gaps).

NOTES:
  - Anything the owner should know that the claims didn't cover.
```

The reviewer owns the READY verdict. The authoring agent must not
self-declare readiness.
