<a id="omnimemeval-2026-10"></a>
# Severian on OmniMemEval: head-to-head results (October 2026)

[Benchmarks index](README.md) · [Earlier internal round](competitor-round-2026-10.md) ·
[Data file](omnimemeval/results-2026-10.json) · [Chart generator](../../scripts/bench_charts.py)

This page is the canonical source for Severian's OmniMemEval numbers. We ran Severian and LCM-X through
[MemTensor/OmniMemEval](https://github.com/MemTensor/OmniMemEval), the public harness behind its
[leaderboard](https://github.com/MemTensor/OmniMemEval/blob/main/docs/user_memory/results.md), with the board's own
answer model, judge model, prompts and metrics. Both systems got identical treatment on every step. The test plan was
written and approved before any paid result existed; every later change to it is listed under
[Changes to the plan](#changes-to-the-plan), including the ones made after results were seen.

**In one paragraph.** On the leaderboard's own setup, Severian 0.3.11 scores **87.34 on LoCoMo, 90.60 on
LongMemEval and 71.62 on BEAM 100K**. Against LCM-X on the same questions it is **ahead on LoCoMo (+7.9) and
LongMemEval (+4.0)** and **level on BEAM (-0.3, a tie)**. A stricter second marker agrees on all three. We also
reproduced LCM-X's own published scores within 0.65 points, so the setup is not tilted against it. Severian's
numbers are self-run, not an official leaderboard entry.

![Severian vs LCM-X](img/h2h.svg)

## Contents

- [Evaluation setup](#evaluation-setup)
- [Result summary](#result-summary)
- [Leaderboard context](#leaderboard-context)
- [LoCoMo](#locomo) · [LongMemEval](#longmemeval) · [BEAM 100K](#beam-100k)
- [Robustness checks](#robustness-checks): reproduction, strict second marker, equal text
- [A marking flaw in BEAM](#a-marking-flaw-in-beam)
- [Severian's own settings](#severians-own-settings) and [what the October improvements did](#improvements-tested)
- [Changes to the plan](#changes-to-the-plan) · [Limitations](#limitations) · [Reproducing](#reproducing)

## Evaluation setup

| Component | Configuration |
| --- | --- |
| Harness | OmniMemEval at [`f7476c5`](https://github.com/MemTensor/OmniMemEval/tree/f7476c5502115542eca9971195bf8c5ca97b6256) (pinned), plus a Severian adapter (`severian_client.py`, follows the Hermes prefetch path: `search` then `assemble_context`) |
| Benchmarks | LoCoMo (1,540 questions, categories 1-4), LongMemEval-S cleaned (500), BEAM 100K (400) |
| Answer model | `openai/gpt-4.1-mini` |
| Judge model | `openai/gpt-4o-mini-2024-07-18` |
| Memory service model | `openai/gpt-4.1-mini` for Severian's write-time fact step; LCM-X has no model step |
| Model route | Nous inference API for every call, both systems (see note) |
| Retrieval depth | top-k 20 for both systems (the board's depth); no character cap |
| Severian | 0.3.11, commit `4467853`, wheel sha256 `b891b2dd…587bea9a`; live switch set (user facts, date stamps, truth resolution, reranker `ms-marco-MiniLM-L6-v2`, conflict notes) |
| LCM-X | v0.28.1, commit `77b2b4f`, OmniMemEval's own `lcmx` adapter, local `BAAI/bge-small-en-v1.5` embeddings |
| Primary metric | LLM-as-a-judge accuracy (LoCoMo, LongMemEval); nugget score (BEAM) |
| Statistics | Paired bootstrap 95% range on the per-question difference (10,000 resamples, seed 20261023) |
| Context tokens | Average answer-prompt tokens per question, as the board reports them |

**Model route.** The board asks for the dated OpenAI models directly. Our OpenAI credit was exhausted, so every
answer, judge and fact call went through the Nous inference API, which serves the same model names. LCM-X's own board
rows were produced through OpenRouter for the same reason, and OmniMemEval discloses that alongside them. Our reproduction
of LCM-X's published scores (below) is the practical check that the route does not move the numbers.

## Result summary

| System | LoCoMo | LongMemEval | BEAM 100K | Context tokens (LoCoMo / LME / BEAM) |
| --- | ---: | ---: | ---: | ---: |
| **Severian 0.3.11** | **87.34** | **90.60** | 71.62 | 5,662 / 6,245 / 15,487 |
| LCM-X v0.28.1 | 79.48 | 86.60 | **71.91** | 1,660 / 5,159 / 11,944 |
| **Severian minus LCM-X** | **+7.86** (+6.04 to +9.74) | **+4.00** (+1.20 to +6.60) | -0.29 (-3.16 to +2.55) | |
| Questions only one system got right | 172 vs 51 | 35 vs 15 | 76 vs 83 | |

A 95% range that stays above zero is evidence of a lead under this method; a range that crosses zero is a tie, not
proof the two are equal. Severian gives the answering model more text: 3.4x LCM-X's on LoCoMo, 1.2x on LongMemEval and
1.3x on BEAM. The [equal-text checks](#equal-text) show how much of the lead survives when both get the same amount.

## Leaderboard context

![Leaderboard context](img/board.svg)

| Backend | LoCoMo | LongMemEval | BEAM 100K | Source |
| --- | ---: | ---: | ---: | --- |
| **Severian 0.3.11** | **87.34** | **90.60** | **71.62** | **self-run, this page** |
| MemOS | 88.83 | 89.20 | 66.87 | board |
| LCM-X | 78.83 | 87.00 | 71.84 | board (contributor-run) |
| EverOS | 82.75 | 80.40 | 58.64 | board |
| Hindsight | 81.99 | 72.20 | 70.22 | board |
| Cognee | 83.48 | 51.80 | 59.30 | board |
| Zep / Graphiti | 63.83 | 79.80 | 68.70 | board |
| mem9 | 73.64 | 78.00 | 65.75 | board |
| Letta | 77.12 | 77.67 | 69.22 | board |
| Mem0 | 77.68 | 56.00 | 70.41 | board |
| Viking | 69.33 | 61.07 | 70.76 | board |
| Supermemory | 73.53 | 66.07 | 65.98 | board |
| MemMachine | 73.90 | 63.60 | 64.80 | board |
| Memori | 41.34 | 20.80 | - | board |

Board rows are copied from OmniMemEval's results page at `f7476c5`. We ran only Severian and LCM-X ourselves, so
these placements are context, not a ranking: we have no paired comparison against MemOS or the others, and Severian's
LongMemEval lead over MemOS (+1.4) is smaller than the paired ranges above. What the table does support is that
Severian's scores sit at the top end of the board on LoCoMo and LongMemEval and level with the top on BEAM.

## LoCoMo

Long conversations between two people; multi-hop, temporal and open-domain questions. 1,540 questions (category 5,
adversarial, excluded as the board does).

| Backend | Single-Hop (841) | Multi-Hop (282) | Temporal (321) | Open-Domain (96) | Overall | Context tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| **Severian 0.3.11** | **93.10** | **80.14** | **84.11** | 68.75 | **87.34** | 5,662 |
| LCM-X v0.28.1 | 84.42 | 67.02 | 80.69 | 68.75 | 79.48 | 1,660 |

Severian's biggest lead is multi-hop (+13.1), the category where an answer needs several turns put together.

## LongMemEval

Long-term interactive memory across up to ~50 sessions per user (LongMemEval-S, cleaned). Overall is the
question-weighted mean.

| Backend | SS-User (70) | SS-Asst (56) | SS-Pref (30) | Temp. Reas (133) | Multi-S (133) | Know. Upd (78) | Overall | Context tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| **Severian 0.3.11** | **100.00** | **98.21** | **100.00** | **88.72** | **79.70** | **94.87** | **90.60** | 6,245 |
| LCM-X v0.28.1 | 97.14 | 94.64 | 93.33 | 85.71 | 74.44 | 91.03 | 86.60 | 5,159 |

Severian is ahead in every category, most on questions spanning several sessions (+5.3).

## BEAM 100K

Very long conversations (~100K tokens each) probing ten memory abilities; nugget score, partial credit per rubric item.

| Dimension (40 each) | Severian 0.3.11 | LCM-X v0.28.1 |
| --- | ---: | ---: |
| Abstention | 82.50 | 82.50 |
| Contradiction resolution | **80.63** | 65.62 |
| Event ordering | 25.47 | **27.97** |
| Information extraction | **90.62** | 89.38 |
| Instruction following | 88.12 | **89.38** |
| Knowledge update | 53.75 | **55.63** |
| Multi-session reasoning | 65.78 | 65.74 |
| Preference following | 88.75 | **94.37** |
| Summarisation | 75.55 | **76.00** |
| Temporal reasoning | 65.00 | **72.50** |
| **Overall** | 71.62 | 71.91 |
| Context tokens | 15,487 | 11,944 |

Severian wins contradiction resolution clearly (+15.0, its truth-resolution feature at work); LCM-X is ahead on
preference following and temporal reasoning. Read the abstention row together with [the marking flaw below](#a-marking-flaw-in-beam).

## Robustness checks

### Reproducing LCM-X's published scores

| Benchmark | LCM-X published (board) | LCM-X in our run | Gap |
| --- | ---: | ---: | ---: |
| LoCoMo | 78.83 | 79.48 | +0.65 |
| LongMemEval | 87.00 | 86.60 | -0.40 |
| BEAM 100K | 71.84 | 71.91 | +0.07 |

Same harness, same adapter, same models: we get LCM-X's own numbers back, so the pipeline treats it as its authors did.

### Strict second marker

The board's judge is lenient (see [BEAM](#a-marking-flaw-in-beam)), so we re-marked the same answers with Claude Opus
using the official LongMemEval per-type templates and, for BEAM, a per-rubric-item score. Nothing was re-answered.
The sample was fixed before any mark: 200 questions per benchmark, stratified by category, the same questions for both
systems. LongMemEval was later extended to all 500 (decided after seeing the sample; disclosed).

![Board vs strict marker](img/strict.svg)

| Benchmark | Questions | Board marker: Severian / LCM-X (difference) | Strict marker: Severian / LCM-X (difference) |
| --- | ---: | --- | --- |
| LoCoMo | 200 | 90.5 / 78.5 (+12.0, +7.5 to +17.0) | 80.0 / 67.5 (**+12.5**, +7.0 to +18.5) |
| LongMemEval | 200 | 90.0 / 87.5 (+2.5, -2.0 to +7.0) | 88.5 / 85.5 (+3.0, -1.5 to +7.5) |
| LongMemEval | 500 (all) | 90.6 / 86.6 (+4.0, +1.2 to +6.6) | 88.4 / 85.0 (**+3.4**, +0.6 to +6.2) |
| BEAM 100K | 200 | 71.5 / 71.5 (+0.0, -4.2 to +4.3) | 60.1 / 58.8 (+1.4, -2.5 to +5.2) |

The stricter marker lowers both systems (LoCoMo by ~11 points, BEAM by ~12) but leaves the comparison where it was:
Severian ahead on LoCoMo and LongMemEval, level on BEAM. The strict marker is another measurement, not ground truth,
and its scores must not be compared with board rows marked by `gpt-4o-mini`.

<a id="equal-text"></a>
### Equal text and a second answering model

A side track used one fixed open reader (`Qwen3.8-27B`, reasoning low) and marker (`DeepSeek-V4-Pro`), and cut each
system's retrieved text to the same token budget before answering. LoCoMo ran on one host (CommandCode); BEAM and
LongMemEval ran entirely on another (Nous), so no comparison mixes hosts. Answers that failed to mark were excluded.

| Benchmark | Arm | Severian | LCM-X | Difference (95% range) | n |
| --- | --- | ---: | ---: | --- | ---: |
| LoCoMo | full text (replicate) | 77.91 | 65.32 | **+12.59** (+10.60 to +14.64) | 1,462 |
| LoCoMo | 1,000 tokens each | 66.50 | 64.50 | **+2.00** (+0.07 to +4.01) | 1,397 |
| LoCoMo | 4,000 tokens each | 77.02 | 65.52 | **+11.49** (+9.61 to +13.44) | 1,488 |
| LoCoMo | 8,000 tokens each | 77.62 | 65.68 | **+11.95** (+9.94 to +13.95) | 1,448 |
| LoCoMo | each system's shipped defaults | 74.00 | 65.36 | **+8.64** (+6.62 to +10.60) | 1,481 |
| LongMemEval | full text (replicate) | 89.9 | 87.1 | **+2.8** (+0.6 to +5.2) | 497 |
| BEAM 100K | full text (replicate) | 65.2 | 67.3 | -2.1 (-5.2 to +1.0) | 391 |
| BEAM 100K | 1,000 tokens each | 51.9 | 53.0 | -1.0 (-4.7 to +2.7) | 386 |
| BEAM 100K | 4,000 tokens each | 63.9 | 66.0 | -2.0 (-5.5 to +1.4) | 381 |
| BEAM 100K | 8,000 tokens each | 65.9 | 67.6 | -1.7 (-4.6 to +1.1) | 388 |
| BEAM 100K | each system's shipped defaults | 59.8 | 67.4 | **-7.6** (-11.4 to -3.9) | 386 |

With a different answering model the picture holds: Severian clearly ahead on LoCoMo at every budget and on
LongMemEval; BEAM a tie at equal text. The one clear loss is BEAM on Severian's **shipped** Hermes defaults (top-10,
8,000 characters): BEAM's long turns overflow that cap. See [Severian's own settings](#severians-own-settings).

## A marking flaw in BEAM

OmniMemEval's BEAM judge scores abstention questions (the right answer is "the chat never said") against a rubric
item and a prompt that asks whether the answer "addresses the criterion". In LCM-X's published BEAM answers, 29 of the
40 abstention answers give a made-up answer instead of saying the information is missing (approximate pattern match),
and the board judge still scores those 29 at 91.4% on average.
The strict marker gives both systems **25.0** on abstention (sample of 20 each), against 82.5 and 80.0 from the board
judge. The flaw inflates every system's BEAM score, ours included; it does not change the Severian vs LCM-X comparison
(level under both markers). We have drafted an issue for the harness maintainers.

## Severian's own settings

The leaderboard setup gives every system 20 memories and no length limit. Hermes ships Severian with 10 memories and an
8,000-character context cap, which keeps every turn cheap. Same stores, same models:

![Settings](img/settings.svg)

| Benchmark | Board depth (top-20, no cap) | Hermes default (top-10, 8,000 chars) | Tokens per question |
| --- | ---: | ---: | --- |
| LoCoMo | 87.34 | 86.17 | 5,662 vs 3,005 |
| LongMemEval | 90.60 | 82.00 | 6,245 vs 2,233 |
| BEAM 100K | 71.62 | 63.86 | 15,487 vs 2,065 |

The cap costs little on LoCoMo (short messages) and a lot on BEAM and LongMemEval, whose memories are long. It is a
deliberate cost/quality trade-off, and the main lever for Severian's everyday BEAM score.

<a id="improvements-tested"></a>
### What the October improvements did

Severian-only before/after runs on the board models, paired on the same questions. All three features ship off by
default; "off" is byte-identical to 0.3.11 (tested).

| Change | Setting | LoCoMo | LongMemEval | BEAM 100K |
| --- | --- | --- | --- | --- |
| New context layout (`SEVERIAN_CONTEXT_FORMAT=v2`) | board depth | 87.34 → 87.60 (+0.26, -0.71 to +1.30) | 90.60 → 90.60 (0.00, -1.80 to +1.80) | not run |
| Fill the spare context + compact citations (`SEVERIAN_FILL_BUDGET`, `SEVERIAN_COMPACT_CITES`) | Hermes default | 86.17 → 87.40 (**+1.23**, +0.13 to +2.34) | 82.00 → 81.80 (-0.20, -2.40 to +2.00) | 63.86 → 64.53 (+0.67, -0.91 to +2.28) |

- The new layout (relevance lead, dated and state-labelled notes, an explicit "say you don't know" rule) made no
  measurable difference. On LongMemEval its 30 abstention questions went from 90.0 to 96.7, too few to count. It stays
  off.
- Filling the spare context with the turns around each hit gives a real LoCoMo gain at the Hermes default and costs
  nothing elsewhere: BEAM and LongMemEval memories already fill the 8,000 characters, so there is nothing to add.
- Two other ideas were measured and rejected: linking memories that name the same person (lost evidence: multi-hop
  "all evidence" 23.8% → 16.7%) and ten faster-reranker variants (each lost evidence on at least one benchmark; best
  search-speed gain under 30%).

<!-- A7-RESULT -->

## Changes to the plan

The plan was written and approved on 22/10/2026, before any paid result. Changes, in order:

| # | When | Change | After results? |
| --- | --- | --- | --- |
| A1-A2 | 22/10 | OpenAI credit unavailable: board models via Nous, side tracks on subscriptions; spend caps; "A beats B" needs the paired range to exclude zero under both markers | No |
| A3 | 23/10 | Subscription limit ran out mid side track: BEAM and LongMemEval side tracks redone entirely on Nous with the same two models (one host per benchmark) | No |
| A4 | 23/10 | Repeat run dropped to fund the improvement tests; second marker kept and sampled (200 per benchmark) | **Yes**, disclosed |
| A4+ | 24/10 | Strict marker extended to all 500 LongMemEval questions | **Yes**, disclosed |
| A5-A7 | 24-25/10 | Severian-only before/after runs (layout; fill + compact at the Hermes default; a higher context cap) | No (each written before its first answer) |

Run incidents (rate limits, a session restart, a GPU memory clash, a side track stopped by its spend cap) are logged
with cause and fix; none changed a reported number. Every paid call went through a capped local gateway that absorbed
rate limits, so no failed model call reached either system.

## Limitations

- **Self-run.** Severian's numbers are ours, not an official board entry. Anyone can rerun them (below).
- **Two systems.** We compared against LCM-X only; other board rows are context.
- **One run.** No repeat run (dropped under A4). Supporting evidence: LCM-X's published scores reproduced within 0.65.
- **Model route.** Nous rather than OpenAI direct (same as LCM-X's own OpenRouter route).
- **Side-track gaps.** LoCoMo equal-text arms exclude 4-9% of answers that failed to mark (repair pass not run); the
  LongMemEval equal-text arms were not completed (spend cap).
- **Not run:** PersonaMem v2, HaluMem, BEAM 10M.
- **Severian does more work at write time.** Its fact step calls the memory model once per saved batch; LCM-X makes no
  model calls. That cost is real and not counted in the context-token column.

## Reproducing

- Harness: OmniMemEval `f7476c5` plus the files in [`omnimemeval/`](omnimemeval/README.md): the Severian adapter, its
  tests, a three-line registration patch, and the optional LCM-X deadline patch, with the exact environment and commands.
- Numbers on this page: [`omnimemeval/results-2026-10.json`](omnimemeval/results-2026-10.json); charts are generated from it
  by [`scripts/bench_charts.py`](../../scripts/bench_charts.py) and a test fails if they drift apart.
