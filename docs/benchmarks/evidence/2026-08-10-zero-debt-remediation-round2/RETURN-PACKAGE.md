# Severian Zero-Debt Round-Two — Return Package

Date: 2026-08-10
Worktree: /home/kensei/repos/severian-zero-debt-r2 (branch zero-debt-remediation-round2)
HEAD: 24e1357c3fbf349243a93cadc0224bf55ebd4bb9 (10 commits above base, tree clean)

## 1. Outcome

All P0/P1/P2 remediation items FIXED with no gate lowering, no blanket
ignores, no fabrication. Model selection: NO MODEL SELECTED (honest,
locked-decision outcome — see below).

## 2. What was fixed (10 commits, exact order per handoff §7)

1. bced61a — P0-A explicit native fallback contract (11/11, RED-proven)
2. dc72d5e — P1-B pure-python + native CI lanes
3. f0ee427 — P1-C mypy --strict 0 (78 files) + P2-I (unwrap, MIT, deps)
4. a1d8de2 — P0-D corrected metric semantics (negative-FP, denominators)
5. 0b0c452 — P0-E frozen semantic corpus + P0-F qwen lane contracts
6. ae31f3b — P1-G/H benchmark index + provenance integrity
7. b34da58 — P1-J supersede invalid R1 evidence, NO MODEL SELECTED
8. acc5078 — fix native runner fixture misuse (found at wheel-enabled run)
9. bde3eac — fix post-truncation normalisation in qwen lanes (bake-off)
10. 24e1357 — round-two evidence package (this package)

## 3. Gate evidence (individual exit codes, all green at bde3eac)

- ruff 0 | mypy --strict 0 (78 files) | compileall 0 | pip-audit 0*
- Pure-Python suite 639/17/0 | Native suite 659/52/0
- Clean CPython 3.11: 90 passed | Clean 3.12: 90 passed
- Live-PG contracts 51 passed
- Cargo fmt/clippy/test/audit/license all 0
- diff-check clean; canonical + R1 candidate untouched; no push

* pip-audit: 2 setuptools CVEs in dev venv fixed via Sahil-approved
  upgrade 2026-08-10; unpublished local packages labelled skips.

## 4. Bake-off (Section 8 — fair, no preselected winner)

- Frozen corpus b836ea6… (1107 docs / 250 queries / 26% non-English /
  40 correction chains / 40 temporal pairs)
- 13 runnable lanes x 3 fresh-process reps; results deterministic
  (recall identical to 4 decimals across reps)
- NO lane passes the frozen gate (recall>=0.90, citation>=0.78,
  unsupported<=0.10, negative-FP<=0.0):
  - best recall 0.8204 — qwen-no-instruction-1024
  - best citation 0.3592 — qwen-0.6b-1024
  - dense models negative-FP 1.000 (retrieve for nonsense probes)
- gemma-300m SKIPPED (licence not authorised — locked decision), so the
  matrix is incomplete BY DESIGN -> NO MODEL SELECTION mandated
- jina-v4 excluded (non-commercial) — never downloaded
- hash baseline honest: 0.7233 recall, 0.049 unsupported, 0.955 neg-FP

## 5. Rust seam (not model-gated)

Vector-only e2e PASS: id parity + bit-exact score parity + 91-95% p95
improvement across 384/512/768/1024. Native crate tests pass, clippy -D
warnings clean, audit 0, MIT licence.

## 6. Deferred (needs Sahil, out of scope)

- Embedding model selection: requires authorising a compliant licence
  (e.g. EmbeddingGemma) or reviewing gate calibration / corpus difficulty,
  then a re-run for selection
- Production activation: NOT touched (no merge, push, rebuild, or config
  change anywhere)

## 7. Evidence location

docs/benchmarks/evidence/2026-08-10-zero-debt-remediation-round2/
- fingerprint.json, test-results.json, README.md (ranked table)
- bakeoff-aggregate.json (sha fbe429dd…)
- bakeoff-reps/rep{1,2,3}/ (full raw lanes; summaries 7f8985bb…,
  816f4b74…, d35eb082…)
- rust-vector-e2e.json (sha 732e4cc…)

## 8. Independent review checklist

- [ ] git log 635a47c..HEAD == 10 commits, tree clean
- [ ] mypy --strict exit 0; ruff exit 0
- [ ] pytest full suites (pure 639 / native 659) 0 failed
- [ ] pip-audit exit 0 (skips labelled)
- [ ] bakeoff aggregate: 13 lanes, all FAIL gate, NO SELECTION
- [ ] canonical repo SHA c4d2b602 untouched; R1 candidate 635a47c untouched
- [ ] no remote push (branch local-only)
