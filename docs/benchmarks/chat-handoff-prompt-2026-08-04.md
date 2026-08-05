# SEVERIAN — SHARE-READY CHAT HANDOFF PROMPT (2026-08-04)

> Copy everything below the line into a FRESH chat with an independent
> auditor/reviewer agent. Self-contained: no prior context needed.
> Producing SHA: 341af8922a712a08522d4b0f8441d9f067de9d57
> (code/evidence final: 9749a75 — commits after it are prompt/docs only)

---

You are an independent auditor reviewing the Severian memory platform
correction lane. Assume every claim below is WRONG until you have
reproduced it yourself. Never trust the repo's own docs — verify against
source and live execution. Do NOT modify source; read-only audit. A
failed command IS the finding. No fabricated numbers.

## Repository state (verify FIRST)

- Repo: `/home/kensei/repos/Severian`
- Branch: `fix/auditor-corrections-2026-08-04`
- HEAD: `341af8922a712a08522d4b0f8441d9f067de9d57`
- Code/evidence final: `9749a750a470d68ce542bb7978554e322d25dc6a` (ancestor;
  the delta 9749a75..HEAD is prompt/docs files only)
- Code baseline: `dfc418f226e7705b4304ae30c9b720fc12ede210`
  (the correction lane is a descendant; the diff baseline..HEAD must be
  limited to: `src/severian/benchmarks/`, `scripts/run_batch_compare.py`,
  `scripts/run_benchmark.py`, `tests/benchmarks/`, `tests/contract/`,
  `docs/` — anything outside that set is a finding)

```bash
cd /home/kensei/repos/Severian
git status --short          # expect: clean
git branch --show-current   # expect: fix/auditor-corrections-2026-08-04
git rev-parse HEAD          # expect: 341af89
git merge-base --is-ancestor dfc418f226e7705b4304ae30c9b720fc12ede210 HEAD && echo "ancestry OK"
git diff --check b628a97..HEAD   # expect: exit 0 (no whitespace)

uv run pytest -q            # expect: 271 passed, 36 skipped
uv run mypy --strict src/severian   # expect: Success, 46 files
uv run ruff check src/severian tests scripts   # expect: All checks passed
```

If ANY of the above fails, stop and report — do not continue.

## What was corrected (reviewer round-2 findings 1-5)

1. **Chroma isolation (C-01, safety-hardened).** Three adversarial defects
   were reproduced and fixed:
   - same workdir + same namespace returned prior-run documents → now a
     cross-process FILE claim marker makes a namespace single-use per
     workdir root (reuse raises ValueError, even across `uv run` processes)
   - `run_namespace='../escaped'` escaped the root → namespaces validated
     as `^[A-Za-z0-9._-]+$` with `.`/`..` explicitly rejected
   - `reset()` deleted an unowned prefix-matching dir → reset() requires
     the ownership marker written only when the adapter created the root;
     pre-existing dirs are refused
2. **Batch speedup (C-02).** `chroma_batch_speedup_vs_perdoc_v2` =
   batch docs_per_sec / per-doc docs_per_sec (was inverted). Zero-denominator
   guard. Legacy inverted field no longer written.
3. **Batch reproducibility (C-03).** Unique benchmark-owned temp roots per
   repetition, `--reps >= 3`, median/spread/samples, git SHA, corpus hash,
   command args, environment fingerprint.
4. **Ranking determinism (C-08).** All ranking tiebreaks now use stable
   keys (content/vector/doc_id) instead of random memory UUIDs. Two clean
   runs are byte-identical (was MRR drifting 0.79-0.88, recall swinging
   0.85-0.92).
5. **Docs + evidence reconciled.** No active doc claims manifest-v1 passes;
   evidence fingerprints point to the final commit.

## Claims to verify

### A. Manifest-v1 quality gate (reproduce ~3-5 min)

```bash
uv run python scripts/run_benchmark.py \
  --manifest docs/benchmarks/manifest-v1.json \
  --pg-dsn "postgresql://severian:severian@127.0.0.1:54329/severian" \
  --chroma-venv /tmp/bench-compare-venv \
  --run-id auditor-v1 --out /tmp/auditor-v1
```

- EXPECTED: `gate_passed: False`; `gate_failures` lists
  `citation_precision` for both severian lanes (0.9375 < 1.0).
- recall 1.000, stale 0.000, leakage 0.000, unsupported 0.000 for both
  severian lanes. The gate honestly FAILS on citation precision only.
- Evidence: `docs/benchmarks/evidence/2026-08-04/result.json`
  (producing SHA 9749a75 in fingerprint.json).

### B. LOCOMO conversation-recall (reproduce ~3-5 min)

```bash
uv run python scripts/run_benchmark.py \
  --manifest docs/benchmarks/manifest-locomo-tier-v1.json \
  --pg-dsn "postgresql://severian:severian@127.0.0.1:54329/severian" \
  --chroma-venv /tmp/bench-compare-venv \
  --run-id auditor-loco --out /tmp/auditor-loco
```

- EXPECTED: severian sqlite AND postgres recall 0.9231, MRR 0.8846,
  cit_prec 0.7188, stale 0.000, leakage 0.000, unsupported 0.1875,
  temporal 1.000. Chroma recall 0.9615, stale 0.1437, leakage 0.000.
- Gate honestly FAILS (recall < 0.95, cit_prec, unsup) — no lane passes.
- Evidence: `docs/benchmarks/evidence/2026-08-04/locomo-*.json`.

### C. Reverse-order isolation (C-01 acceptance)

Run manifest-v1 then LOCOMO vs LOCOMO then manifest-v1 into the SAME
Chroma root (`--chroma-workdir /tmp/order-shared`, unique `--run-id` each).
- EXPECTED: identical non-latency metrics + returned IDs; no `d*` IDs in
  LOCOMO traces; Chroma cross_scope_leakage 0.000.
- Evidence: `docs/benchmarks/evidence/2026-08-04/orderA-*` and `orderB-*`.

### D. Batch speedup (reproduce ~1 min)

```bash
uv run python scripts/run_batch_compare.py \
  --chroma-venv /tmp/bench-compare-venv \
  --docs 200 --reps 3 --out /tmp/auditor-batch
```

- EXPECTED: `chroma_batch_speedup_vs_perdoc_v2` > 1 (batch faster than
  per-doc); legacy field absent; reps 3; median/spread/samples; git_sha
  = current HEAD; corpus_sha256 present.
- Running WITHOUT `--chroma-venv` must exit 2 (argparse).
- Evidence: `docs/benchmarks/evidence/2026-08-04/batch-compare.json`.

### E. Safety (adversarial — do not skip)

```bash
# 1. Same workdir + same namespace reuse must raise ValueError
uv run python -c "
import sys; from pathlib import Path
site = next((Path('/tmp/bench-compare-venv')/'lib').glob('python*/site-packages'), None)
if site: sys.path.insert(0, str(site))
from severian.benchmarks.adapters.chroma import ChromaAdapter
import tempfile, shutil
w = tempfile.mkdtemp(prefix='adv-')
try:
    a = ChromaAdapter(provider_id='c', workdir=w, run_namespace='ns'); a.close()
    try:
        ChromaAdapter(provider_id='c', workdir=w, run_namespace='ns')
        print('FAIL: reuse not blocked')
    except ValueError:
        print('OK: reuse blocked')
finally:
    shutil.rmtree(w, ignore_errors=True)
"

# 2. Path traversal namespace must raise ValueError
#    run_namespace='../escaped'  -> ValueError; no dir outside root created

# 3. reset() must refuse an unowned prefix-matching dir
#    mkdtemp(prefix='severian-bench-chroma-') + user file, adapter with
#    that workdir, reset() -> PermissionError; user file survives
```

### F. Determinism (reproduce, ~5 min)

Run the LOCOMO benchmark TWICE with different `--run-id` and fresh
`--out`. EXPECTED: byte-identical metrics and returned-ID order across
both runs for all four lanes.

### G. Wheel

```bash
uv venv /tmp/auditor-wheel && uv pip install --python /tmp/auditor-wheel/bin/python \
  dist/severian-0.1.0-py3-none-any.whl
/tmp/auditor-wheel/bin/severian --help   # must list ingest, search, rollback, episode
```

## Honest limitations (documented, not hidden — verify they exist in
`docs/HANDOFF-MASTER-2026-08-03.md` §8)

1. A-3 real-data lane: spec written, NOT run (blocked on corpus approval).
2. B-1/LOCOMO gate fails honestly (recall 0.923 < 0.95).
3. Manifest-v1 gate fails honestly on citation precision (0.9375 < 1.0).
4. Cold-derive scale curve (9.9s@1k → 6458s@100k) — measured design
   consequence; worker + idempotent rebuild mitigate.
5. Vendor-claim policy: cited vendor results are vendor-reported, never
   claimed as internal reproductions.
6. No-secrets verification: repo-tree grep for sk-*/api_key=/password=/
   Bearer found none (bounded scan scope).

## Time-box guidance

| Lane | Time | Action |
|---|---|---|
| State verification | ~2 min | MUST run |
| Manifest-v1 gate (A) | ~3-5 min | MUST run |
| LOCOMO (B) | ~3-5 min | MUST run |
| Reverse-order (C) | ~6 min | MUST run |
| Batch (D) | ~1 min | MUST run |
| Safety (E) | ~2 min | MUST run |
| Determinism (F) | ~5 min | MUST run |
| Wheel (G) | ~1 min | MUST run |
| 100k scale lane | hours | SKIP — verify provenance only (evidence/2026-08-03/) |
| Full Granite lane | hours | SKIP — verify historical vs spot-check distinction (granite-comparison doc) |

## Deliverable format

```
VERDICT: READY | NEEDS CORRECTION | NOT READY   (one line)

VERIFIED:
  - [claim] reproduced: command -> output summary

REFUTED / QUESTIONABLE:
  - [claim] expected X, got Y (command, output)

GAPS:
  - [claim] not verified because <reason> (time/env/credentials)

NOTES:
  - Anything the owner should know that the claims didn't cover.
```

The reviewer owns the READY verdict. The authoring agent must not
self-declare readiness.
