# WORKTODO — from the Agent Memory Atlas review

**Source:** <https://neoneye.github.io/agent-memory-atlas/systems/severian/>
**Reviewed commit:** `bdb14b04` (2026-11-09) — the same commit as this repo's `0.3.13` pin.
**How it was produced:** one LLM reviewer reading the code at that commit. The
reviewer states it installed and ran nothing, screened for auto-run surfaces and
found two build-time execution paths (`native/build.rs`, `tests/conftest.py`) it
did not trigger. Every claim below is therefore a *reading*, not a test result,
and must be re-proved before it is treated as fact.
**Rubric:** the atlas scores 7 mechanisms. Severian carries 4 — `scope_enforced`,
`audit_log`, `bitemporal`, `negative_eval`. Not found: `tombstone`, `trust_state`,
`human_review`. A dash means "not found at this commit", not "needed".
**Status:** captured as candidate work. Nothing here is approved, scheduled or
started. Triage each row before building.

---

## W-1 — "Forget" removes from reads, not from storage  (HIGH)

`erase_observation` writes a `[erased]` tombstone as a revision and supersedes the
original. Each memory derived from it is erased, relations are retracted, the
projection is rebuilt. Search, context and indexes stop returning it — asserted by
`test_erased_observation_leaves_no_trace_in_search`.

But the original row keeps `content` and `raw_content`: `revise_observation` only
updates `superseded_by_id` (`sqlite.py:598-648`). The text therefore survives in
the SQLite file, in a PostgreSQL row, and in a `severian export` snapshot. The
docstrings justify it as history kept "for audit".

**Gap:** a deployment that must honour a deletion request needs a purge path the
tree does not provide. The atlas's "avoid" list is blunt: *don't call it erasure
if the text stays*.

**Candidate work:** a real purge (row + export + snapshot) that keeps only the
fact that something was removed; document the current behaviour honestly in the
meantime.

## W-2 — Correction is first order only  (HIGH — matches local finding t_54fb0317)

Correcting an observation cascades one hop and is proven. Nothing links a memory
to another memory, so a plan or belief built on a corrected memory stays active
and keeps asserting the stale claim; search returns the corrected fact and the
stale plan side by side. `severian_correct(memory_id=…)` cascades nothing.

**Status:** documented in `docs/memory-engine.md` (scope A of t_54fb0317, 14/11/26).
**Candidate work (scope B/C, not approved):** link memory→memory, or withdraw
dependent memories on correction. Any change is a write-path change on the shared
store.

## W-3 — Five payload changes write no audit row  (HIGH)

Audit rows are inserted in the same transaction for create, revision and
rollback, and consolidation appends `dreaming.merge`. Nothing updates or deletes
an audit row. But these paths change a payload with no audit row:

1. `supersede_memory`
2. `close_memory`
3. `supersede_memories_for_source`
4. `retract_relations_for_source`
5. (the fifth path the reviewer lists in the same group — re-confirm which)

Consequence: an erasure shows in the log only as the tombstone's `memory.create`.
`CurationService` writes a marker memory saying *"review proposals in audit"* but
writes no audit entry, and the worker discards the report it returns
(`worker.py:453`).

**Candidate work:** audit rows for all five paths; either make the curation
marker's claim true or remove it.

## W-4 — Dreaming is a job kind with no producer  (MEDIUM)

`ConsolidationService`, `CurationService` and `RetentionPolicyService` only run
when the worker drains a job of kind `dream` with a `mode`
(`worker.py:448-463`). Every `enqueue_job` call in `src/` and `scripts/` queues
`derive`; the only `dream` jobs in the tree are written by tests, and the scale
lanes call the services directly.

**Gap:** the README's "Looks after itself" describes code a deployment does not
run. Either schedule the dream jobs or correct the README.

## W-5 — MCP hands the model all five scope dimensions  (MEDIUM, security)

Scope is enforced in storage: `_scope_predicates` applies all five dimensions to
every canonical read, `_require_scope` refuses a blank one, cross-scope `get_*`
raises `NotFoundError`, and `assemble_context` fails closed. The limit is *who
picks* the scope. `_resolve_mcp_scope` would pin tenant and profile to a bound
principal — but `python -m severian.mcp` constructs the server without one, so
over stdio the model supplies all five.

**Candidate work:** bind a principal at launch, or document stdio MCP as
single-tenant/trusted-context only.

## W-6 — Relation validity is record time, not event time  (MEDIUM)

`Relation` carries `valid_from`/`valid_to`, and single-valued predicates
(`works_at`, `located_in`, `based_in`) end an older relation when a newer one
arrives. `valid_to` is set from the wall clock at write (`sqlite.py:1236-1262`),
and the only producer, `EntityExtractionService` (`extraction.py:247`), passes no
validity. So relation history is "when Severian saved it", which is exactly the
distinction the memory path is careful about.

**Candidate work:** derive relation validity from the source's event time, as the
memory path does.

## W-7 — Index bookkeeping competes for the context budget  (LOW/MEDIUM)

The atlas flags indexing the engine's own bookkeeping: a summary memory listing
merged ids, or a review marker at confidence 1.0, spends the character budget
that facts need.

**Candidate work:** keep maintenance artefacts out of the searchable set.

## W-8 — Benchmark evidence is only partly reproducible  (MEDIUM, credibility)

`docs/benchmarks/omnimemeval/results-2026-10.json` holds per-category scores and
counts. Category-weighted means recompute to **87.33** LoCoMo (stated 87.34),
**90.60** LongMemEval, **71.62** BEAM 100K. The file names the harness commit, the
Severian commit `4467853` — **35 commits before this pin, 799 lines of `src/`
changed since** — the answer model `gpt-4.1-mini` and judge `gpt-4o-mini`.

The per-question files live under `~/data/benchmarks/h2h-p1` and are **not
committed**, so paired bootstrap ranges cannot be recomputed from the tree. The
write-up also reports re-running LCM-X within 0.65 of its published scores with
no paper cited.

**Candidate work:** commit the per-question evidence (or a manifest + hashes), and
re-run at a commit someone can check out.

## W-9 — Missing mechanisms the rubric looked for  (assess)

Not found at this commit: `tombstone` (see W-1 — `:erased` is the near-miss),
`trust_state`, `human_review`. There is no approval/review queue surface anywhere
in `src/` or `web/src`. `TruthState` is computed at read time and never stored
(`models.py:24-35`) — which is fine, but means nothing carries candidate/
verified/rejected.

**Candidate work:** decide whether any of the three is wanted. If not, say so in
the README so the absence is a decision rather than an omission.

## W-10 — Test coverage gaps implied by the above  (HIGH)

1,203 test functions in 125 files, with contract tests against both backends,
adversarial repository cases, snapshot integrity, interface parity, auth, CLI and
the harness. Nothing pins:

- second-order correction behaviour (W-2) — because it does not exist;
- that erasure leaves no text in the row/export (W-1) — the existing test only
  proves it leaves no trace *in search*;
- audit rows for the five paths (W-3);
- a `dream` job producer (W-4);
- MCP scope binding without a principal (W-5).

**Candidate work:** add the tests that would fail today, then fix or explicitly
waive each.

## W-11 — Keep these (the atlas's "steal" list, already true here)

Re-confirm each with a test rather than assuming the reader got it right:

1. Derivation queued in the same transaction as the evidence — no observation
   without its job, and the revision path does the same.
2. Projections converge to canonical state, not to event order — a late create
   event cannot re-index a since-corrected memory.
3. Close instead of overwrite, dated by the statement's own time, so `as_of`
   reads stay honest for imported histories.
4. Blank scope is a storage error, and a cross-scope context hit fails closed.
5. The audit is read-only: it cannot write a byte, and repair only re-queues and
   re-indexes.

## W-12 — Reviewer's own limits  (context, not work)

The atlas page says explicitly that the marks are one reviewer's reading of the
code, not a run, and that a dash means "not found", not "missing". Treat every
item above as a lead to verify, not a verdict. Two build-time execution paths were
noted and not triggered.

## W-13 — Rebuild the memory→wiki promotion job against Severian  (REQUESTED)

**Why it exists.** A daily job used to take the most reliable stored facts and copy
the good ones into the GBrain wiki at `~/brain/`, so they became browsable notes
instead of chat memory. It read Mnemosyne, which has been removed entirely
(14/11/26), so the job was deleted. Sahil wants it rebuilt for Severian.

**Archived original** (full definition, for reference):
`~/.hermes/governance/archive-retired-cron/memory-promotion-daily.json`
Schedule was `35 4 * * *`; silent when there was nothing to promote.

**What the original did, and what has to change:**

| original (Mnemosyne) | rebuild (Severian) | note |
|---|---|---|
| `mnemosyne_recall(query="*", limit=30)`, filter `importance >= 0.8` | Severian has no `importance`; it has `confidence` (default derivation writes 0.5) | **decision needed:** either map the bar to `confidence` and lower it, or promote on a different signal (user-fact memories, revised/corrected facts, usage_count). At 0.8 nothing would ever pass. |
| one implicit store | five-part scope (tenant/profile/collection/agent/session) | **decision needed:** which scope(s) the job reads. Hermes uses `tenant_id=kensei, profile_id=default, collection_id=profile, agent_id=default, session_id=shared`. |
| `veracity=stated or imported` | Severian kinds: `fact`, user-fact extractions, consolidated/curated kinds | map "stated" to the derivation kinds that represent a user assertion, not a synthesis |
| read `~/brain/RESOLVER.md`, match to an existing page, skip if already covered | unchanged | keep the page-matching + dedupe logic |
| borderline → `~/.hermes/governance/memory-promotion-review.md` (no kanban cards) | unchanged | Sahil approved this on 16/10: 13 of 13 promotion cards were never actioned |
| `patch` the brain page, never overwrite; brain only, never the Obsidian vault | unchanged | |
| `[SILENT]` when nothing to promote | unchanged | |

**Acceptance:**
- Runs daily, reads Severian read-only, and promotes only facts that match an
  existing brain page and are not already there.
- A dry-run mode prints what it would promote, with ids, without writing.
- Tested against the live store in dry-run before it is enabled; first real run
  reviewed by hand.
- Nothing is written to the Obsidian vault; brain pages are patched, never
  rewritten wholesale.
- Borderline facts land in the review file, never as kanban cards.

**Status:** not started. Requires the two decisions above before any code.

**Handoff note (Sahil, 14/11/26):** do **not** build this as part of the current
tidy-up. It is left here deliberately for another agent to pick up from this file.
Whoever takes it should:

1. Read the archived original at the path above before writing anything — it is the
   behavioural spec, not a starting point to redesign.
2. Get the two open decisions answered first: the promotion signal/threshold
   (Severian has no `importance`; its default confidence is 0.5, so the old 0.8 bar
   promotes nothing) and which scope(s) the job reads
   (Hermes default is tenant `kensei`, profile `default`, collection `profile`,
   agent `default`, session `shared`).
3. Build with a dry-run mode and prove it against the live store read-only before
   the job is enabled. Leave the cron paused until the first real run has been
   reviewed by hand.
4. Update this item with the commit, the dry-run output and the enabling decision.



---

### Triage table

| id | item | severity | status |
|----|------|----------|--------|
| W-1 | erasure keeps text | high | todo |
| W-2 | second-order correction | high | documented only |
| W-3 | five unaudited paths | high | todo |
| W-4 | dream jobs have no producer | medium | todo |
| W-5 | MCP scope chosen by model | medium | todo |
| W-6 | relation validity is record time | medium | todo |
| W-7 | bookkeeping in the index | low | todo |
| W-8 | benchmark evidence not reproducible | medium | todo |
| W-9 | missing mechanisms | assess | todo |
| W-10 | tests for all of the above | high | todo |
| W-11 | strengths to keep proven | n/a | re-confirm |
| W-12 | reviewer limits | n/a | context |
| W-13 | rebuild memory→wiki promotion on Severian | medium | requested, 2 decisions needed |
