# Severian — Superiority Handover (2026-08-03)

**Status:** Working document — answers to Sahil's comments a/b/d + the
interrogation prompt for research-led superiority.
**Owner:** KENSEI (Severian programme)
**Inputs:** benchmark results (docs/benchmarks/results.md, evidence/2026-08-03/),
LLM-wiki research synthesis (~/wiki/concepts/*.md), competitor vendor claims
(results.md §5).

---

## Part 1 — Answers to Sahil's comments

### (a) The weaknesses need addressing — what the LLM-wiki research says

Measured weaknesses (independently reproduced, evidence/2026-08-03/):

| # | Weakness | Evidence |
|---|---|---|
| W1 | Cold derive super-linear: 9.9s@1k → 149.5s@10k → 6,458s@100k (sqlite) | scale-100k.json |
| W2 | SQLite hot path degrades at 100k: p95 3.2s (PG stays ~150ms) | scale-100k.json |
| W3 | No semantic embeddings — hash+BM25 only | results.md §6 |
| W4 | Ingest ~156–254 docs/s (vs baseline 300k/s) | scale-1k-10k.json |
| W5 | No agent-loop / conversation-level benchmark run (LOCOMO, BEAM, EvoArena) | results.md §8 |

Research-validated take-aways, each mapped to a weakness:

- **BM25 is NOT the problem — it is validated.** Wiki page
  `bm25-wins-at-scale-rag` (paper 2607.26497): BM25 remains competitive or
  superior to dense/neural retrieval at scale and is far cheaper; the paper's
  recommendation is **hybrid: BM25 for recall + lightweight neural reranker
  for precision**. Severian's FTS5+BM25 base is aligned with the research;
  the missing piece is the reranker tier (W3 fix, below).
- **Semantic embeddings are the single highest-leverage upgrade.**
  `memdelta-controlled-memory-evaluation` (2606.29914): swapping ONLY the
  embedding model shifts accuracy by +6.2pp (p=0.004); Mem0 beats MiniLM-RAG
  by +11pp but loses to cloud-RAG by 1.2pp — i.e. embedding choice flips the
  ranking. The fix is a pluggable embedding provider; wiki page
  `ibm-granite-embedding-r2` gives us the concrete candidate: Granite R2 97M,
  Apache-2.0, 60.3 MTEB retrieval (best sub-100M), 384-dim, 32K context,
  CPU-runnable (ONNX/OpenVINO) — local-first compatible. (W3)
- **Super-linearity of cold derive is a design consequence, not a bug — but it
  must be engineered around.** `agent-native-memory-evaluation` (2606.24775):
  "localized maintenance > global reorganization"; incremental updates beat
  full reindexing. Severian's production worker model (derive=False, async
  per-scope) IS the localized-maintenance answer — the benchmark already shows
  PG hot path ~150ms p95 at 100k. The cold 100k number (6,458s) is the
  synchronous full-rebuild path, which production does not use. (W1, W2)
- **Version control / rollback is a real differentiator we can add.**
  `chronomem-version-control` (2607.27773): ALL major memory systems are
  forward-only; versioning + semantic rollback is genuinely missing
  everywhere. Severian already has observation supersession; adding version
  inspection + rollback would be a competitive moat, not parity. (W1/W2 area —
  mitigates cold-rebuild risk)
- **Budgeted compression is the scale answer.** `memrefine` (2606.13177):
  LLM-judge merge/delete/preserve under a storage budget. Relevant once
  memory grows beyond a corpus — a retention policy tier. (W4/W5 area)
- **Conversation-level recall is the right next benchmark tier.**
  `evoarena` (2606.13681): LoCoMo +4.8% from patch-based memory (EvoMem);
  `agent-native-memory-evaluation`: 12 memory systems evaluated on 5
  workloads — the evaluation framework exists and is reproducible. (W5)
- **Ghost-memory / supersession is where we already win and can stay ahead.**
  `atma-ghost-memory` (2607.01935): the exact failure Chroma exhibits
  (stale 0.179) is called "ghost memory"; A-TMA's three-level fix (bank
  maintenance, retrieval filtering, answer-time resolution) maps directly to
  what Severian already implements — this is our proven differentiator.

**Net answer to (a):** The research validates our architecture (BM25 base,
supersession, localized maintenance) and names three concrete upgrades:
(1) pluggable semantic embeddings (Granite R2 97M as local-first candidate),
(2) BM25+reranker hybrid retrieval, (3) version control/rollback layer. All
three are roadmap items with seams already in the codebase (provider adapter
interface, projection layer, observation supersession). None requires a
redesign.

### (b) Measuring ourselves against vendor claims — yes, with a strict rule

Position: vendor claims are legitimate comparison targets even if we suspect
they are inflated — BUT we never claim a win we haven't independently measured
on our own harness, and we never pass off a vendor number as ours.

Rule (must hold for every claim we publish):

1. **Same manifest, same harness, same scope.** Every comparison uses
   manifest-v1.json + our runner. Competitors run through our adapters
   (chroma is executed; Mem0/Hindsight/etc. are vendor-reported-only with
   citations — results.md §5).
2. **We measure ourselves against their CLAIMED dimensions.** If a vendor
   claims X on LOCOMO, we don't take their word and we don't attack a strawman
   — we build the LOCOMO-tier into our benchmark (adapter protocol already
   supports it) and measure our own number on the same query taxonomy. Then we
   can honestly say "we scored Y on LOCOMO-tier; vendor claims X" — even if we
   only ever verify our own Y.
3. **If we are better, we say so with the evidence. If we are not, we say so.**
   The whole point of an honest benchmark is that it can lose. Current honest
   losses: ingest throughput vs baseline; cold derive at 100k; semantic
   retrieval (unmeasured vs cloud embeddings — we currently don't have
   embeddings, so we do NOT claim to match semantic search yet).
4. **No fabricated parity.** A vendor claim we cannot reproduce or refute is
   recorded as vendor-reported with a citation, never as a "we beat them"
   claim. WE MUST NOT LIE — the benchmark's value is precisely that it is the
   one source of truth.

Concrete next action (after this handover is consumed): add a
`competitor-claims.md` table — each vendor claim, our measured number on the
same dimension, and the verdict (ahead / behind / unverifiable). Populate from
the evidence + manifest. This directly feeds marketing claims that are
defensible.

### (d) Conversation recollection — yes, Severian must compete there

Sahil's expectation is right: a memory system that cannot recall a past
conversation ("what did we decide about X last week?") is not a memory system,
it is a fact store. Competing on conversation recollection is a requirement,
not an option.

Current state:
- Severian's domain model has observations (immutable source records) and
  derived memories — the raw material for conversation recall exists.
- The 5-D security boundary includes session_id — conversation scoping is
  already first-class.
- What is MISSING: (1) an agent-loop / conversation benchmark tier (LOCOMO /
  EvoArena-style) — we deliberately did not run these (results.md §8), so we
  have no measured conversation-recall number; (2) a conversation-synthesis
  / episodic summary path (MemRefine-style) to make long sessions retrievable
  as episodes, not 10k raw observations.

Vendor landscape (vendor-reported only): Hindsight (passive chat capture),
Honcho (conversation grounding), SuperMemory (chat memory), Mem0 (agent-loop
LOCOMO claims). They all lead on the conversation narrative. To compete we
need:

1. **Add the benchmark tier** — run a LOCOMO-style or EvoArena-style workload
   through our own harness so we have an honest conversation-recall number.
   This is the "vendor claims" test from (b) applied to d.
2. **Add episodic summarization** — the MemRefine/EvoMem pattern: budgeted
   consolidation of session observations into episode summaries, so recall is
   fast and conversation history stays correct (supersession at observation
   level still applies).
3. **Claim only what we measure** — after (1) and (2) we can state
   conversation recall with evidence; before then, no claim.

---

## Part 2 — Interrogation prompt: what to take from LLM-wiki research +
competitor docs/code to achieve superiority

Copy the block below into a fresh agent session (or hand to a research lead).
It is self-contained.

---

### PROMPT (self-contained)

You are the research-and-architecture lead for Severian, a proprietary
local-first memory platform. Your mission: identify, from the LLM-wiki
research synthesis and from competitor documentation/code, the specific
techniques, designs, and benchmarks that Severian must adopt to achieve
demonstrated superiority across ALL tiers and metrics — then produce an
evidence-backed acquisition roadmap.

**Context files to read first:**
- /home/kensei/repos/Severian/docs/benchmarks/results.md — current measured
  position (independent + vendor-reported + extrapolated, clearly separated)
- /home/kensei/repos/Severian/docs/benchmarks/manifest-v1.json — comparison
  manifest and provider specs
- /home/kensei/repos/Severian/docs/benchmarks/evidence/2026-08-03/ — raw
  measured evidence
- /home/kensei/repos/Severian/src/severian/benchmarks/adapters/ — adapter
  protocol (severian.py, chroma.py, baseline.py) — the harness itself

**Research synthesis sources (LLM-wiki):**
Read every page in /home/kensei/wiki/concepts/ and /home/kensei/wiki/comparisons/
whose tags include agent-memory, memory, retrieval, RAG, benchmark, embedding,
compression, or conversation. At minimum these pages exist and are relevant:
- bm25-wins-at-scale-rag (2607.26497)
- memdelta-controlled-memory-evaluation (2606.29914)
- agent-native-memory-evaluation (2606.24775)
- atma-ghost-memory (2607.01935)
- chronomem-version-control (2607.27773)
- memrefine (2606.13177)
- evoarena (2606.13681)
- danus-fact-graph-memory (2607.06447)
- ibm-granite-embedding-r2
- sigma-memory-reliability, memprobe-memory-recovery-benchmark,
  latent-context-language-models, rate-distortion-memory-compaction,
  filesystem-memory-llm-agents, gui-agent-active-memory-atmem
Cross-reference the raw papers under /home/kensei/wiki/raw/papers/ for any
claim you intend to rely on.

**Competitor sources:**
- ChromaDB (the only independently reproduced competitor): inspect the
  adapter (src/severian/benchmarks/adapters/chroma.py) and, if installed,
  the chroma codebase at /tmp/bench-compare-venv — identify what Chroma does
  well that Severian does not (batch ingest, real embeddings, ANN index).
- Vendor-reported competitors (Mem0, Hindsight, Honcho, SuperMemory,
  Mempalace, Mnemosyne): collect their public docs/claims as listed in
  results.md §5. For each, extract the claim, the benchmark they cite, and
  whether that benchmark is reproducible (LOCOMO, BEAM, EvoArena, etc.).

**Your task — produce a superiority roadmap document answering:**

1. **Tier-by-tier gap analysis.** For each functional tier of a memory system
   (ingest/write path, representation, extraction, retrieval+routing,
   maintenance/update, conversation recall, operations/security), state:
   Severian's current measured position, the best-in-class position from
   research or competitors, and the delta. Every claim must cite either a
   measured evidence file or a wiki page + paper.
2. **Benchmarks to acquire.** Which public benchmarks can Severian run on its
   own harness to produce defensible comparison numbers? For each: name,
   what it measures, expected runtime, feasibility with the current adapter
   protocol, and which competitor claims it lets us honestly address.
   Prioritise conversation-recall benchmarks (LOCOMO/EvoArena style).
3. **Techniques to adopt, ranked by leverage.** From the research synthesis,
   rank the concrete techniques Severian should implement (e.g. pluggable
   embeddings — Granite R2 97M; BM25+reranker hybrid; version control +
   semantic rollback; budgeted episodic compression; patch-based memory).
   For each: what it fixes, the wiki/paper source, the competitor it
   neutralises, implementation seam in the Severian codebase, effort, and
   expected impact on measured metrics.
4. **What NOT to copy.** Explicitly list competitor/research features that are
   traps (e.g. cloud-only embedding dependence, non-deterministic defaults,
   per-doc write paths, features that conflict with the 5-D security boundary
   or local-first determinism).
5. **The superiority claim set.** Draft the concrete, defensible claims
   Severian will be able to make after the roadmap is executed — each one a
   testable statement with the metric and the benchmark that would verify it.
   Mark each as: can claim now / can claim after benchmark X / can claim
   after implementation Y.

**Output format:** a single markdown document, max 250 lines, no fluff:
one table per tier, one ranked list per section, explicit citations
(wiki page name + paper ID) for every claim. End with a 10-line executive
summary: the three highest-leverage acquisitions and the one benchmark that
would most change our competitive position.

---

## Part 3 — How to use these two handovers

1. **Immediate (today):** third-party evaluation prompt
   (handover-third-party-eval-2026-08-03.md) → spin a fresh session or hand to
   an auditor → confirm our current claims are true before we build on them.
2. **Next (this week):** Part 2 prompt → research lead (Remii profile when API
   limits allow; inline otherwise) → superiority roadmap document.
3. **Then:** turn roadmap into kanban tasks (benchmarks first, then techniques
   by leverage ranking), each with exit criteria tied to measured metrics.
