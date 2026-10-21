# Competitor round, October 2026

Severian against four other memory systems on three public long-term-memory benchmarks, every system run the same
way on the same machine. Results as of 18/10/2026; Severian version benchmarked: 0.3.6 (recommended settings below).

## Headline (share of questions answered correctly, marked by Claude Opus)

| System | LoCoMo (199 q) | BEAM 100K (100 q) | LongMemEval (60 q), 5,000 chars | LongMemEval, 1,500 chars |
|---|---|---|---|---|
| **Severian 0.3.6** | **59.8%** (52.8-66.3) | **58.3%** (50.5-66.1) | **66.7%** (55.0-78.3) | 43.3% (31.7-56.7) |
| Severian, features off | 57.8% (50.8-64.8) | 55.7% (48.0-63.7) | 53.3% (40.0-65.0) | 36.7% (25.0-48.3) |
| Mnemosyne | 48.2% (41.2-55.3) | 47.9% (39.8-56.1) | 51.7% (38.3-63.3) | 43.3% (30.0-55.0) |
| Mem0 | 53.3% (46.2-60.3) | 28.4% (21.0-36.0) | 46.7% (33.3-60.0) | **48.3%** (36.7-61.7) |
| Hindsight | 43.2% (36.7-50.3) | 40.8% (33.1-48.7) | not run [e] | not run [e] |
| Honcho | 26.1% (20.1-32.2) | 37.6% (30.1-45.4) | not run [e] | not run [e] |

Brackets are 95% bootstrap intervals (10,000 resamples). Severian against the best other system on the same questions
(paired difference):

- **BEAM** vs Mnemosyne: **+10.4 points** (+2.6 to +18.4), a clear win.
- **LongMemEval at 5,000 characters** vs Mem0: **+20.0 points** (+5.0 to +35.0), a clear win; vs Mnemosyne +15.0
  (0.0 to +30.0).
- **LoCoMo** vs Mem0: +6.5 points (-1.0 to +14.1), ahead.
- **LongMemEval at 1,500 characters**: level with Mem0 (-5.0, -20.0 to +10.0) and Mnemosyne (0.0).

## How close to perfect recall?

To separate the memory system from the answering model, we also handed the answering model exactly the conversation
turns the datasets label as holding each answer ("best possible"), and nothing at all ("no memory").

| | LoCoMo (199) | LongMemEval (60) |
|---|---|---|
| Best possible (labelled evidence only) | 59.8% | 76.7% |
| **Severian 0.3.6** | **59.8%** | **81.7%** with no size limit; 66.7% at 5,000 chars |
| No memory | 0.0% | 5.0% (the 3 "I don't know" questions) |

Severian matches the labelled-evidence score on LoCoMo and passes it on LongMemEval. The main reason is its date
stamps: Severian writes the real date next to "yesterday" or "last Friday" when it saves a turn, so time questions
can be worked out (LoCoMo time questions: 79% against 49% for the labelled turns alone). The no-memory row shows the
questions cannot be answered from general knowledge.

## The common rule

- Retrieval only, top 10 memories per question, the same answering prompt for every system.
- One local answering model for everyone (Qwen3.8-27B), and one local extraction model (Carwin) for systems that
  extract facts. Scores sit below vendors' published numbers mainly because of this model: vendors publish with large
  cloud models.
- Every answer marked by Claude Opus (BEAM per rubric item); 48 of 50 Opus marks matched a hand check. Differences of
  1-3 points are ties.
- Competitors run under this rule, not at their vendors' recommended settings: Mem0 publishes at 200 memories (here
  10), Hindsight returns a token budget cut here to 10 items, Honcho publishes through its own answering agent. Read
  their rows as a lower bound.

## Footnotes

- [a] Severian, Severian-off and Mnemosyne LoCoMo/BEAM answers are from 11-12/10; Mem0, Hindsight and Honcho re-runs
  from 16-17/10, after an answering-server update (same weights). Measured drift on LongMemEval: about +3 points for
  Severian, 0 for Mnemosyne and Mem0. The LongMemEval columns are all from one server on one night.
- [b] Lost work, counted rather than hidden: 158 of Severian's LongMemEval fact batches failed (mostly the extraction
  model refusing LongMemEval content) and 1 BEAM batch; Mem0 had 3 (BEAM) and 18 (LongMemEval) unreadable extraction
  replies; Hindsight gave up one merge step covering 8 memories on BEAM; Honcho split 2 over-long BEAM messages. No
  system lost a conversation, and the model server never crashed.
- [e] Hindsight and Honcho need more than a day each to save the LongMemEval conversations, so they were not run.

## Recommended settings (as benchmarked)

`SEVERIAN_USER_FACTS=1` (short facts per turn), `SEVERIAN_DATE_STAMPS=1`, `SEVERIAN_TRUTH_RESOLUTION=1`,
`SEVERIAN_OPT_NUMPY=1`, `SEVERIAN_RERANK=cross-encoder/ms-marco-MiniLM-L6-v2`, Qwen3-Embedding-0.6B vectors.
"Severian, features off" is the same code with only `SEVERIAN_OPT_NUMPY=1`.

## Since this round

- **0.3.9** turns on a note when two returned memories disagree. Re-test on 21/10 (same harness, Opus marks): +1.9
  points on BEAM with no size limit and +2.2 at 8,000 characters, no change elsewhere, never lower in total.
- In progress: joining facts across conversations (LoCoMo multi-hop), saying "I don't know" when nothing fits (BEAM
  abstention) and facts that change over time (knowledge update). These are the categories where another system still
  scores higher; see the gap list below.

| Category | Severian 0.3.6 | Best other |
|---|---|---|
| LoCoMo multi-hop | 26% | Mem0 42% |
| BEAM abstention | 40% | Mem0 80% |
| BEAM knowledge update | 70% | Mnemosyne 85% |
| BEAM contradiction resolution | 24% | Honcho 30% |
| Search speed (median, LoCoMo) | 63 ms | Honcho 43 ms |

Category cells hold 10-43 questions each, so single categories are noisy.
