# Severian benchmark results — October 2026

Status: canonical results index for the October 2026 OmniMemEval round. Every
number below is tied to the version and configuration that produced it. The
detailed tables, per-system rows and limitations are in
[omnimemeval-2026-10.md](omnimemeval-2026-10.md).

## Headline

Severian was run through OmniMemEval at pin
`f7476c5502115542eca9971195bf8c5ca97b6256` with the board answer model
`openai/gpt-4.1-mini` and judge `openai/gpt-4o-mini-2024-07-18`. This is our run
through the public harness, not an official board entry.

| Benchmark | Questions | Severian 0.3.11, board config | LCM-X v0.28.1 | Difference (95% paired range) |
|---|---:|---:|---:|---:|
| LoCoMo | 1,540 | **87.34** | 79.48 | **+7.86** [+6.04, +9.74] |
| LongMemEval | 500 | **90.60** | 86.60 | **+4.00** [+1.20, +6.60] |
| BEAM 100K | 400 | 71.62 | 71.91 | -0.29 [-3.16, +2.55] |

**Severian configuration for the table:** 0.3.11, commit `4467853`, wheel
sha256 `b891b2dd…587bea9a`; board depth (top-20, no character cap); the live
switch set (user facts, date stamps, truth resolution, reranker
`ms-marco-MiniLM-L6-v2`, conflict notes); local Qwen3-Embedding-0.6B. Token
averages are 5,662 / 6,245 / 15,487 per question.

**LCM-X configuration:** v0.28.1, commit `77b2b4f795fb6a4f84c55abf6d0441da4469971e`,
OmniMemEval's `lcmx` adapter, local `BAAI/bge-small-en-v1.5` embeddings.

The differences are paired per-question bootstrap ranges (10,000 resamples,
seed 20261023); they are not error bars on an absolute leaderboard score. The
board itself publishes no ranges.

## Versions and configurations

| Severian setting | LoCoMo | LongMemEval | BEAM 100K |
|---|---:|---:|---:|
| 0.3.11, board depth: top-20, no cap | 87.34 | 90.60 | 71.62 |
| 0.3.11, Hermes default: top-10, 8,000-char cap | 86.17 | 82.00 | 63.86 |
| 16,000 chars + fill + compact, top-10 | not run | 86.4 | 69.54 |

The Hermes-default row is the shipped setting, not the leaderboard setting.
The 16,000-character row is the pre-registered higher-limit side test; LoCoMo
was not run because its memories do not fill 8,000 characters.

**Version control:** 0.3.12 and 0.3.13 are byte-identical to 0.3.11 with their
switches off (CHANGELOG), so the results stay labelled **0.3.11**. No number is
relabelled to a newer version without a source diff and a new run.

## The 0.01 LoCoMo reconciliation

The published LoCoMo headline is 87.34. Two routes give a 0.01 difference:

- **Pooled correct answers:** category counts 841 + 282 + 321 + 96 = 1,540.
  The category scores imply 783 + 226 + 270 + 66 = **1,345 correct**. That is
  `1345 / 1540 = 87.338…`, which rounds to **87.34**.
- **Weighting rounded category percentages:** `(841×93.1 + 282×80.14 +
  321×84.11 + 96×68.75) / 1540 = 87.335…`, which rounds to **87.33**.

The difference is rounding in the already-rounded category display, not a
scoring error. The per-question recompute returns **87.34**.

## NDCG inflation audit — PASS

`src/severian/benchmarks/runner.py:154-165` builds the ideal DCG from **all
expected items** (`sorted(expected_weights.values(), reverse=True)[:k]`), not
from the retrieved top-k. A retriever cannot inflate NDCG by returning fewer
items. The OmniMemEval headline numbers use no NDCG, so this audit does not
change 87.34 / 90.60 / 71.62.

## Reproduce it

The per-question archive is
`~/data/benchmarks/_records/omnimemeval-2026-10/results-per-question.tar.zst`
(sha256 `303e2486850257ad39a94315b3ceae480e5a36f92cc51803cfa29c1f928f34be`).
`sha256sum -c SHA256SUMS` passes for every file in that directory (verified
20 November 2026). The archive contains 3,437 files.

```bash
python docs/benchmarks/omnimemeval/recompute-2026-10.py \
  --archive ~/data/benchmarks/_records/omnimemeval-2026-10/results-per-question.tar.zst
```

The script has no network and makes no model or paid calls. It verifies the
committed file manifest
`docs/benchmarks/omnimemeval/MANIFEST-results-per-question-2026-10.sha256`
(sha256 `4703170bb5e30366fcbb0027b8fc6ea45ec8d6190f7bc3b3444bc496993b4563`),
recomputes every judged row, and fails if any headline, category count/mean or
paired range differs from
[omnimemeval/results-2026-10.json](omnimemeval/results-2026-10.json).
Expected final line:

```text
PASS: 87.34 / 90.60 / 71.62 and all paired ranges and category rows match results-2026-10.json
```

If the archive is already extracted, pass `--extracted-root <dir>` instead of
`--archive` (the manifest is still verified).

## What is not committed

The judged per-question files are **not** committed. The archive does not carry
the LoCoMo, LongMemEval-S or BEAM dataset licence texts, and redistribution
permission for these derived outputs has not been established. This repository
commits the manifest, hashes and recompute script only, so the numbers remain
reproducible without redistributing third-party data.

## Limits

- One run of each benchmark; not an official OmniMemEval board entry.
- The BEAM judge has a documented marking flaw that rewards made-up answers to
  "the chat never said" questions; it inflates every system's BEAM score.
- BEAM 10M, PersonaMem v2 and HaluMem were not run.
- Paired ranges describe question-sampling uncertainty for this run, not
  model-version or judge-version uncertainty.
- The board uses a single answer model and a single judge; the strict-marker
  cross-check is in the detailed page.
- Any new result needs its own version, commit and configuration label.

## Index

- [omnimemeval-2026-10.md](omnimemeval-2026-10.md) — full per-benchmark tables,
  LCM-X reproduction, strict marker, equal-text tracks and limitations.
- [METHODOLOGY.md](METHODOLOGY.md) — layers, fairness rules, configurations and
  reproduction.
- [competitor-round-2026-10.md](competitor-round-2026-10.md) — earlier internal
  round against Mem0, Mnemosyne, Hindsight and Honcho.
- [history/](history/) — August-era instruments, retained as provenance only.
