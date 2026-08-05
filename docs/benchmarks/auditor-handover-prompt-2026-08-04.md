# SEVERIAN v0.1.0 — INDEPENDENT AUDITOR HANDOVER PROMPT (2026-08-04)

> Author: KENSEI
> Purpose: objective, adversarial assessment of the Severian memory platform
> by an independent auditor. Assume every claim below is WRONG until you have
> reproduced it yourself. Never trust the repo's own docs — verify against
> source and live execution.
> Repo: `/home/kensei/repos/Severian` — branch `fix/auditor-corrections-2026-08-04`
> — HEAD `c8c4809` (code baseline `dfc418f`; the correction lane is a
> descendant of it)
> (verify before anything else; if HEAD differs, audit the checked-out state
> and report the delta).

---

## 0. Your mandate

You are an independent auditor. Deliver:
1. **VERIFIED** — claims you reproduced and confirmed.
2. **REFUTED / QUESTIONABLE** — claims that failed or could not be
   reproduced, with the exact command/output that disproved them.
3. **GAPS** — things you could not verify (time, environment, missing
   credentials), each stated as a gap, NOT silently skipped.
4. One-line verdict: READY / NEEDS CORRECTION / NOT READY.

Honesty rules:
- A failed command IS the finding. Never fix the repo to make a claim pass.
- No fabricated numbers. If a number cannot be reproduced, say so.
- Do NOT modify source. Read-only audit. (Exception: you may create scratch
  files under /tmp and disposable PostgreSQL state via the documented reset
  guard.)
- Do NOT run long scale lanes (hours). The 100k bucket is pre-existing
  evidence; verify its provenance, do not re-run it. That skip is a
  documented finding, not an omission.

---

## 1. Project identity

Severian = standalone local-first memory platform. Canonical store
(observations/memories/entities/relations as append-only JSONB-ish records),
FTS5 lexical index, vector projection (hash embeddings default, Granite R2
pluggable), derivation worker, versioned CLI + HTTP API + MCP surface.
Five-dimensional scope boundary: `tenant_id / profile_id / collection_id /
agent_id / session_id`. Local-first, deterministic, offline-capable.

Repos:
| Repo | Branch | HEAD (claimed) | Status |
|---|---|---|---|
| `/home/kensei/repos/Severian` | `fix/auditor-corrections-2026-08-04` (correction lane; main is the merged baseline) | `c8c4809` | clean |
| `/home/kensei/repos/ultramemory-dashboard` | `audit/brutal-critique-v0.1.0` | `ce4796d` | clean (out of scope unless you choose to extend) |

CODE BASELINE (C-06): the audited code baseline is `dfc418f`
(`dfc418f226e7705b4304ae30c9b720fc12ede210`). The correction lane adds:
benchmark isolation (C-01), batch corrections (C-02/C-03), deterministic
tiebreaks (C-08), plus tests and docs. Verify ancestry: the audit checkout
must be a descendant of `dfc418f` and the intervening diff must be limited
to `src/severian/benchmarks/`, `scripts/run_batch_compare.py`,
`scripts/run_benchmark.py`, `tests/benchmarks/`, `docs/`, and
`tests/contract/` — anything outside that set is a finding. Do NOT hardcode
a single expected HEAD: assert ancestry + allowed-diff, not an exact SHA.

Environment:
- PostgreSQL 16 + pgvector at `postgresql://severian:severian@127.0.0.1:54329/severian`
  (DSN gated by `SEVERIAN_PG_DSN`; destructive reset gated by
  `SEVERIAN_PG_ALLOW_RESET=1` — the benchmark scripts set it for the
  dedicated benchmark DSN).
- Chroma venv at `/tmp/bench-compare-venv` (chromadb, throwaway).
- Wheel: `dist/severian-0.1.0-py3-none-any.whl` + sdist.

---

## 2. State verification protocol (run FIRST — before reading any docs)

Run these from `/home/kensei/repos/Severian`. Record actual vs expected.

```bash
# 1. Repo state (C-06: ancestry + allowed-diff, not exact HEAD)
git rev-parse HEAD            # must be a DESCENDANT of dfc418f226e7705b4304ae30c9b720fc12ede210
git merge-base --is-ancestor dfc418f226e7705b4304ae30c9b720fc12ede210 HEAD && echo "ancestry OK"
git diff --name-only dfc418f226e7705b4304ae30c9b720fc12ede210..HEAD | cat
                              # must be limited to: src/severian/benchmarks/,
                              # scripts/run_batch_compare.py, scripts/run_benchmark.py,
                              # tests/benchmarks/, tests/contract/, docs/
git status --short            # expected: empty (clean)

# 2. Test gate — expected: 271 passed, 36 skipped, 0 failed
uv run pytest -q 2>&1 | tail -3

# 3. Type gate — expected: "Success: no issues found in 46 source files"
uv run mypy --strict src/severian 2>&1 | tail -2

# 4. Lint gate — expected: "All checks passed!"
uv run ruff check src/severian tests scripts 2>&1 | tail -2

# 5. Manifest validation — both must validate
uv run python -m severian.benchmarks.cli validate --manifest docs/benchmarks/manifest-v1.json
uv run python -m severian.benchmarks.cli validate --manifest docs/benchmarks/manifest-locomo-tier-v1.json

# 6. Wheel installs in a fresh venv + CLI works
uv venv /tmp/auditor-wheel && uv pip install --python /tmp/auditor-wheel/bin/python \
  dist/severian-0.1.0-py3-none-any.whl
/tmp/auditor-wheel/bin/severian --help   # must list ingest, search, rollback, episode
```

If ANY of the above fails, stop and report — do not continue onto claims
verification against a broken base.

---

## 3. Claims to verify

Each claim has: expected value (from evidence files / source) and the
evidence path. Mark each VERIFIED / REFUTED / NOT-REPRODUCED.

### 3.1 Release-gate claims (manifest-v1.json — the standing quality gate)

The release gate has FIVE configured thresholds (read them from the
manifest's `release_gate` block — do not trust prose):
`min_recall_at_10=0.95`, `min_citation_precision=1.0`, `max_stale_fact_rate=0.0`,
`max_cross_scope_leakage=0.0`, `max_unsupported_answer_rate=0.0`,
plus `require_external_providers=1`.

- CLAIM: On manifest-v1.json, Severian lanes achieve recall@10 1.000,
  stale_fact_rate 0.000, unsupported_answer_rate 0.000, but FAIL the gate on
  citation_precision (0.938 < 1.0). The overall gate is honestly
  `gate_passed: False` with `gate_failures: {severian-sqlite:
  [citation_precision], severian-postgres: [citation_precision]}`.
  Reproduce (the runner writes result.json with `gate_passed`):
  ```bash
  uv run python scripts/run_benchmark.py --manifest docs/benchmarks/manifest-v1.json \
    --chroma-venv /tmp/bench-compare-venv \
    --pg-dsn "postgresql://severian:severian@127.0.0.1:54329/severian" \
    --out /tmp/auditor-v1
  ```
  Expected: `gate_passed: False` (citation_precision fails for both severian
  lanes; all other thresholds pass); 4 lanes in evidence (sqlite, postgres,
  chroma, baseline-lexical).
  IMPORTANT — stale committed evidence: `docs/benchmarks/evidence/2026-08-02/`
  is an OLD pre-fix run (recall 0.933, cit_prec 0.103, stale 0.145–0.159, no
  git_sha field; last touched by 56b5dc9) and does NOT reflect current main.
  Treat the FRESH run you produce as the source of truth; the committed file
  is a provenance finding (stale evidence should be re-stamped or removed).
  NOTE: this run is ~minutes, not hours — reproduce it.

### 3.2 LOCOMO-tier conversation-recall claims (B-1)

- CLAIM: On manifest-locomo-tier-v1.json, Severian measures recall@10 0.923,
  MRR ~0.79–0.81, citation_precision 0.656, stale_fact_rate 0.000,
  unsupported_answer_rate 0.188, temporal_accuracy 1.000 for BOTH sqlite and
  postgres lanes. Reproduce:
  ```bash
  uv run python scripts/run_benchmark.py --manifest docs/benchmarks/manifest-locomo-tier-v1.json \
    --chroma-venv /tmp/bench-compare-venv \
    --pg-dsn "postgresql://severian:severian@127.0.0.1:54329/severian" \
    --out /tmp/auditor-locomo
  ```
  Compare against committed evidence:
  `docs/benchmarks/evidence/2026-08-03-locomo-tier/result.json`
  (SHA256 `e459507c…` — verify the committed file's hash first).
  PROVENANCE NOTE: the committed evidence records producing commit `75b61fc`
  (fingerprint.json). Current HEAD `dfc418f` includes e341705 (a
  behavior-identical refactor: temporal fallback now calls the explicit
  `list_observations_history` alias). Expect the fresh run to match within
  float noise; any material delta is a finding.
- CLAIM: The gate honestly FAILS for this corpus (recall 0.923 < 0.95).
  The runner's `gate_failures` must list `recall_at_10`,
  `citation_precision`, `unsupported_answer_rate` for severian lanes.
- CLAIM: Chroma also fails the gate (stale_fact_rate 0.100,
  citation_precision 0.469) — no provider passes this corpus.
- CLAIM: residual failures are scoped: q6 (semantic paraphrase gap — hash
  cannot match "secrets stored now" to c27), q9 (adversarial decoy — c17 is
  in-scope), q11 (hash-collision noise — addressable via opt-in
  `min_fused_score`). Trace file: `…/traces.json`.

### 3.3 Granite R2 embedding comparison (A-1)

- CLAIM: Granite R2 (107M multilingual, Apache-2.0, 384-dim, CPU) is a
  pluggable `EmbeddingProvider` (dimension + encode) — default remains
  HashEmbedding (deterministic SHA-256, no process-seed dependence).
  Inspect `src/severian/infrastructure/granite_embedding.py` and
  `src/severian/infrastructure/vector.py` (EmbeddingProvider protocol).
- CLAIM: On the LOCOMO corpus, Granite finds the paraphrase-distant target
  (q6 → c27) where hash returns []. **Provenance (C-04 — do not conflate):**
  the HISTORICAL full-lane trace
  (`docs/benchmarks/evidence/2026-08-03-locomo-granite/traces.json`)
  records q6 → `[]` for both Granite lanes; a CURRENT bounded spot-check
  (producing SHA c8c4809) returned `[c27, c12, c24]` with c27 at rank 1.
  These are DIFFERENT evidence statements. The full-lane negative result
  (Granite recall 0.808/0.885 vs hash 0.923) is unchanged.
  Optional reproduce (model download ~500MB, CPU; time-box 10 min):
  ```bash
  uv run python -c "
  from severian.benchmarks.adapters.severian import SeverianAdapter
  from severian.infrastructure.granite_embedding import GraniteR2Embedding
  emb = GraniteR2Embedding(); emb.load()
  a = SeverianAdapter(provider_id='g', backend='sqlite', embedding=emb,
                      min_fused_score=None, min_vector_score=0.0)
  import json; from pathlib import Path
  m = json.loads(Path('docs/benchmarks/manifest-locomo-tier-v1.json').read_text())
  for d in m['corpus']['documents']:
      a.ingest(str(d['id']), str(d['content']), scope=str(d['scope']),
               supersedes=d.get('supersedes') or [])
  r = a.query('How are secrets stored now?', limit=10, scope='t1/p1/c1/a1/s1',
              query_kind='correction')
  print([i['document_id'] for i in r.items][:3])
  "
  ```
  Expected: c27 appears in the top 3 (with no floor).
- CLAIM (honest negative): on the full LOCOMO corpus Granite does NOT beat
  hash — recall 0.808/0.885 vs 0.923, unsupported 0.250 vs 0.188 (evidence:
  `…-locomo-granite/result.json` vs `…-locomo-tier/result.json`). This
  negative result is intentionally recorded. Do NOT re-run the full Granite
  lane (model + embedding of 30 docs is slow); verify the evidence file
  exists and its numbers are internally consistent, and mark the re-run
  as not-reproduced-this-session if you skip.

### 3.4 Chroma batch-ingest comparison (A-4)

- CLAIM: `ingest_batch` protocol exists; Chroma adapter overrides it with a
  vectorized upsert; Severian's per-doc path is ~8x faster than Chroma's
  batch path. Measured: chroma per-doc 2.1 docs/s, batch 22.8 docs/s,
  severian 184.4 docs/s. Evidence:
  `docs/benchmarks/evidence/2026-08-03-batch-compare/batch-compare.json`
  (SHA256 `02ad338c…`). Reproduce (fast, ~1 min):
  ```bash
  uv run python scripts/run_batch_compare.py --chroma-venv /tmp/bench-compare-venv \
    --docs 200 --out /tmp/auditor-batch
  ```

### 3.5 Scale evidence provenance (E-5)

- CLAIM: `docs/benchmarks/evidence/2026-08-03/scale-1k-10k.json` records
  git_sha = the producing commit (9566937-era re-run at HEAD) and corpus sha
  `2e432878…`; `scale-100k.json` records the producing commit `e6ead47` with
  a provenance_note documenting verification at `9566937`.
  Inspect both files' `git_sha` / `provenance_note` fields. This is a
  provenance claim — do NOT re-run the 100k lane (hours).

### 3.6 Domain/repository behavior claims (E-1/E-2/E-3, A-2, B-2)

- CLAIM: list_memories / list_memories_history / list_audit order by
  created_at (payload JSON), id tiebreak — NOT UUID order. Inspect
  `src/severian/infrastructure/sqlite.py` and `postgresql.py`.
- CLAIM: export/import enforce an approved-root path policy (symlink
  rejection before resolve, traversal normalization, overwrite guards).
  Inspect `src/severian/cli/path_policy.py`; tests in
  `tests/unit/test_path_policy.py`.
- CLAIM: domain models reject empty content and enforce SHA-256 hash format.
  Inspect `src/severian/domain/models.py`; tests `tests/unit/test_models.py`.
- CLAIM: `rollback_memory(scope, revision_id)` (ChronoMem-style, A-2)
  restores a historical revision as active head; implemented in both
  backends + service; tests `tests/integration/test_memory_engine.py`
  (test_rollback_memory_*).
- CLAIM: episodic summarization (B-2): EpisodeService consolidates a
  session's observations into Memory(kind="episode"); worker 'episode' job
  kind; CLI `severian episode`. Tests `tests/application/test_episodes.py`.
- CLAIM: projection rebuild is idempotent — digest cache skips full rebuild
  when canonical unchanged. Inspect `src/severian/application/projections.py`;
  test `tests/application/test_service.py::test_rebuild_is_idempotent_*`.

---

## 4. Honest-limitations checklist (verify these are DOCUMENTED, not hidden)

Confirm each of the following appears in
`docs/HANDOFF-MASTER-2026-08-03.md` §8 with a straight face:
1. A-3 real-data lane: spec written, NOT run (blocked on corpus approval).
2. B-1 gate fails honestly (0.923 < 0.95) — no lane passes the LOCOMO gate.
3. manifest-v1 gate also fails honestly on citation_precision (0.938 < 1.0)
   for both severian lanes — recall/stale/unsup all pass.
4. Cold-derive scale curve (9.9s@1k → 6458s@100k) is a measured design
   consequence; worker + idempotent rebuild mitigate; no parity claim.
5. Vendor-reported numbers are cited, never claimed as internal
   (`docs/benchmarks/competitor-claims.md`).
6. No credentials/secrets anywhere (grep the repo for `sk-`, `api_key=`,
   `password=`, `Bearer ` — report any hit).

---

## 5. Document map (read in this order after state verification)

| File | Role |
|---|---|
| `docs/HANDOFF-MASTER-2026-08-03.md` | Master register + locked decisions + completion status §8 |
| `docs/plans/2026-08-03-complete-a-g-execution.md` | A–G execution tracking |
| `docs/benchmarks/results.md` | Aggregate benchmark results |
| `docs/benchmarks/manifest-v1.json` | Quality gate manifest (5 thresholds) |
| `docs/benchmarks/manifest-locomo-tier-v1.json` | LOCOMO conversation corpus (30 docs, 16 probes) |
| `docs/benchmarks/locomo-tier-analysis-2026-08-03.md` | B-1 analysis + q7 root-cause correction note |
| `docs/benchmarks/granite-comparison-2026-08-03.md` | A-1 honest negative result |
| `docs/benchmarks/competitor-claims.md` | C-1 vendor claims table |
| `docs/benchmarks/real-data-probe-spec-2026-08-03.md` | A-3 lane spec (not run) |
| `docs/benchmarks/evidence/2026-08-03-locomo-tier/` | B-1 evidence (result/traces/fingerprint) |
| `docs/benchmarks/evidence/2026-08-03-locomo-granite/` | A-1 Granite evidence |
| `docs/benchmarks/evidence/2026-08-03-batch-compare/` | A-4 batch evidence |
| `docs/benchmarks/evidence/2026-08-03/` | E-5 scale evidence (provenance) |
| `src/severian/` | Source (application/infrastructure/domain/benchmarks/cli) |
| `tests/` | 255 tests + 36 skips |

---

## 6. Time-box guidance

| Lane | Time | Action |
|---|---|---|
| State verification (2.1–2.6) | ~2 min | MUST run |
| manifest-v1 gate (3.1) | ~2–5 min | MUST run |
| LOCOMO-tier (3.2) | ~3–5 min | MUST run |
| Batch compare (3.4) | ~1 min | MUST run |
| Granite q6 spot-check (3.3) | ~10 min | OPTIONAL (model download) |
| Granite full lane | hours | SKIP — verify evidence only |
| Scale 100k lane | hours | SKIP — verify provenance only |

---

## 7. Deliverable format

```
VERDICT: READY | NEEDS CORRECTION | NOT READY   (one line)

VERIFIED:
  - [claim id] reproduced: command → output summary

REFUTED / QUESTIONABLE:
  - [claim id] expected X, got Y (command, output)

GAPS:
  - [claim id] not verified because <reason> (time/env/credentials)

NOTES:
  - Anything the owner should know that the claims didn't cover.
```

*This prompt was built from live verification of the repo at dfc418f. Every
command above has been run exit-0 by the author in this session.*
