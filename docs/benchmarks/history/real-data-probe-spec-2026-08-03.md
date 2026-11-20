# A-3 — Real-data probe specification (2026-08-03)

**Status:** SPEC WRITTEN — lane not yet run (requires a real corpus source).
**Register link:** HANDOFF-MASTER-2026-08-03 §A-3.
**Honest position:** the 27-doc quality corpus (manifest-v1.json) and the
30-doc conversation corpus (manifest-locomo-tier-v1.json) are synthetic
gates. They prove mechanism correctness but are NOT universal truth. A
real-data probe measures Severian against actual production-shaped memory
traffic. This document specifies that lane so it can be run when a corpus
source is approved.

## Why this exists

- Synthetic corpora have known structure: clean facts, complete scope
  fields, no duplication, no partial revisions, no interleaved sessions.
- Real memory traffic has: repeated observations, contradictory updates,
  partial information, mixed modalities, punctuation/typo noise, scope
  drift, long-tail vocabulary, and genuine ambiguity.
- The honest claim is "measured on synthetic gates" until a real-data lane
  exists. No vendor comparison may claim real-world parity without one.

## Corpus source options (ranked by honesty/effort)

| Source | What it gives | Effort | Risk |
|---|---|---|---|
| 1. Sahil's own session logs (Hermes) | Real agent memory traffic: observations, corrections, session boundaries | Low (export from session DB) | PII — must be scrubbed; approved-root + redaction gate |
| 2. Public agent-trajectory datasets (e.g. SWE-bench trajectories, AgentBench logs) | Real task-oriented memory | Medium (parse + scope mapping) | Licence check; format mapping |
| 3. Synthetic-but-adversarial (adversarial-27) | Deterministic hard cases: typos, paraphrase, contradictions, decoys | Low (extend manifest-v1) | Still synthetic — honest label required |

**Recommended:** option 1 (Sahil's own logs) for the first real-data lane —
it is the highest-fidelity match to the actual use case (agent memory for
Sahil's workflows) and the export path already exists (session DB + FTS).

## Lane design (same manifest/runner/scope policy)

- Manifest id: `real-data-v1-2026-XX` (date when corpus is captured).
- Documents: N real observations (target 500–5,000 after dedupe/scrub),
  each mapped into the five-D scope.
- Queries: derived from actual follow-up questions found in the logs +
  the 7 LOCOMO query kinds. Expected evidence = the observation(s) the
  human/agent actually referenced next.
- Providers: severian-sqlite, severian-postgres, chroma (same harness).
- Metrics: identical to manifest-v1 (recall@10, MRR, nDCG, cit_prec,
  stale, temporal, unsupported, latency).
- Gate: same thresholds as manifest-v1.

## Blocker / precondition

A real-data lane requires **approval to use Sahil's session logs** as a
corpus (privacy + scope decision) OR a licence-cleared public dataset.
Until then the lane stays SPEC-only — this is an honest limitation, not a
fabricated completion.

## Verification steps when the lane runs

1. Corpus captured + scrubbed (PII redaction proof: no emails/phones/keys
   in the exported JSON — grep + hash check).
2. Manifest validates (`validate --manifest real-data-v1.json`).
3. Full harness run (4 providers), evidence written with git_sha + corpus
   sha.
4. Results added to results.md with the real-data label; the synthetic
   gates remain the release gate (real-data is informational until
   thresholds are met 3 consecutive runs).
5. HANDOFF-MASTER A-3 updated to DONE with evidence paths.

*Recorded 2026-08-03. Honest-numbers policy: no real-world claim until
this lane produces a number.*
