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

**Owner direction (20/11/26):** Sahil approved proceeding with the Must + Should
recommendations of the MoSCoW review. Could items are captured below
(W-14..W-18) and are NOT scheduled. The implementation plan goes to an
independent objective review BEFORE any build; nothing here is implemented yet.
- Decision record: `~/docs/severian-programme/MOSCOW-DECISION-RECORD-2026-11-20.md`
- Implementation plan: `~/docs/severian-programme/IMPLEMENTATION-PLAN-2026-11-20.md`
- Review kickoff: `~/docs/severian-programme/handoffs-2026-11-20/KICKOFF-REVIEW.md`
Must items map to plan M1–M7, Should items to S1–S4; the triage table at the
bottom carries the 20/11 disposition of every row.

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

**Disposition 20/11:** MUST — plan M1 / REV A1, MG-2, MG-3, MG-7 (W-10 tests).
BUILT and independently reviewed 20/11 on `fix/must-integrity-2026-11-20`:
explicit service + CLI purge, atomic erase, fresh restatement after forget,
strict purge-aware snapshot round trips and truthful physical-cleanup status.
Task notes remain out; external backups and PG reclaim are not performed.
Full SQLite: 1617 passed / 119 skipped; full disposable PG: 1716 / 20;
ruff and mypy clean. Local-only; no live-store access or deployment.
Evidence: `~/docs/severian-programme/evidence/must-should-2026-11-20/M1-KENSEI-ACCEPTANCE.md`.
Original claim and RED receipts are preserved there. Wave gate (§3.9) pending;
M3 has not started.

## W-2 — Correction is first order only  (HIGH — matches local finding t_54fb0317)

Correcting an observation cascades one hop and is proven. Nothing links a memory
to another memory, so a plan or belief built on a corrected memory stays active
and keeps asserting the stale claim; search returns the corrected fact and the
stale plan side by side. `severian_correct(memory_id=…)` cascades nothing.

**Status:** documented in `docs/memory-engine.md` (scope A of t_54fb0317, 14/11/26).
**Candidate work (scope B/C, not approved):** link memory→memory, or withdraw
dependent memories on correction. Any change is a write-path change on the shared
store.

**Disposition 20/11:** WON'T this iteration (decision record N1). Sahil chose
scope A (document only) on 14/11; live exposure near zero (2 `observation.revise`
events ever, 0 memory-path corrections, 0 active memories with a superseded
source). Reopen only on owner call or real exposure.

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

**Disposition 20/11:** MUST — plan M2. Four paths re-proved 20/11 (no audit write
in method bodies). Fifth most likely `record_memory_usage` (sqlite l.946,
PG l.1091) — confirm at build by sweeping every payload-mutating method.

**Corrected 20/11 by the independent review (REV §2.1 A2, plan rule 12 — REV wins
over WT).** The true list of payload-mutating writes with no audit row is SIX, not
five, and the fifth-path guess was wrong:

1. `close_memory`
2. `supersede_memory`
3. `supersede_memories_for_source`
4. `retract_relations_for_source`
5. `_end_replaced_relations` (ends the older single-valued relation; called from
   `publish_derivation`)
6. `publish_derivation`'s update of an EXISTING relation (support or reopen)

**Exempt, reason recorded:** `record_memory_usage` (the old fifth-path guess).
Nothing in `src/` passes `search(track_usage=True)`, so it never runs in
production; auditing it would write audit rows on the read path. Revisit only if
S2 turns usage tracking on deliberately.
**Out of scope:** task notes (`task_notes.py`) — working state, not canonical
memory.

**Status:** BUILT 20/11 on `fix/must-integrity-2026-11-20` (plan v2 M2, evidence
`must-should-2026-11-20/M2.md`). Every path above now writes one append-only
audit row per affected target in the same transaction, through a new audit-only
in-transaction helper (never `_insert_events_in_transaction`, which would append
an outbox event and raise `KeyError` on an unknown op). Actions: `memory.close`,
`memory.supersede`, `memory.source_superseded`, `relation.retract`,
`relation.unsupport`, `relation.end`, `relation.support`, `relation.reopen`,
`curation.report`. The MG-4 stale-write race is fixed in the same item:
`supersede_memory` is now one conditional UPDATE of the current payload guarded on
active state and the full scope. The curation marker's "review proposals in
audit" claim is now true: the pass persists a `curation.report` row (ids and
counts, never content) and the worker no longer discards the report.

## W-4 — Dreaming is a job kind with no producer  (MEDIUM)

`ConsolidationService`, `CurationService` and `RetentionPolicyService` only run
when the worker drains a job of kind `dream` with a `mode`
(`worker.py:448-463`). Every `enqueue_job` call in `src/` and `scripts/` queues
`derive`; the only `dream` jobs in the tree are written by tests, and the scale
lanes call the services directly.

**Gap:** the README's "Looks after itself" describes code a deployment does not
run. Either schedule the dream jobs or correct the README.

**Disposition 20/11:** split. MUST (minimum) — plan M4: correct the README claim
now. SHOULD — plan S2: deterministic-first dreaming producer (schedule the jobs,
deterministic triage before any LLM, retention trail default-on). Claim re-proved
20/11: only `derive` enqueued in src/scripts; dream modes handled at worker.py:448.
**Built 20/11 (M4):** README and memory-engine now say no production path queues
dream jobs, the usage term is inert and salience ranking is opt-in; local commit
`021e627`. S2 producer remains open.

## W-5 — MCP hands the model all five scope dimensions  (MEDIUM, security)

Scope is enforced in storage: `_scope_predicates` applies all five dimensions to
every canonical read, `_require_scope` refuses a blank one, cross-scope `get_*`
raises `NotFoundError`, and `assemble_context` fails closed. The limit is *who
picks* the scope. `_resolve_mcp_scope` would pin tenant and profile to a bound
principal — but `python -m severian.mcp` constructs the server without one, so
over stdio the model supplies all five.

**Candidate work:** bind a principal at launch, or document stdio MCP as
single-tenant/trusted-context only.

**Disposition 20/11:** MUST — plan M3. Re-proved 20/11: `mcp/server.py:495`
accepts `principal` (SEC-01) but `mcp/__main__.py` never passes it. Fail-closed
launch with explicit trusted-single-tenant opt-out recommended (plan Q1).
**Built 20/11:** launch requires `--tenant-id`/`--profile-id` or
`SEVERIAN_MCP_TENANT_ID`/`SEVERIAN_MCP_PROFILE_ID`, or explicit
`--trusted-single-tenant`; tenant/profile are pinned, collection/agent/session
remain caller-chosen. RED/GREEN live stdio evidence in the programme folder;
controller M3 gate passed 20/11 (SQLite 1635/119 skipped, PG 1734/20 skipped,
ruff + mypy clean). Local commit `be12e08`; independent review not required by
the owner for M3 and not performed.

## W-6 — Relation validity is record time, not event time  (MEDIUM)

`Relation` carries `valid_from`/`valid_to`, and single-valued predicates
(`works_at`, `located_in`, `based_in`) end an older relation when a newer one
arrives. `valid_to` is set from the wall clock at write (`sqlite.py:1236-1262`),
and the only producer, `EntityExtractionService` (`extraction.py:247`), passes no
validity. So relation history is "when Severian saved it", which is exactly the
distinction the memory path is careful about.

**Candidate work:** derive relation validity from the source's event time, as the
memory path does.

**Disposition 20/11:** SHOULD — plan S3, plus temporal conformance tests
(mixed-tz instants, inclusive boundaries, accumulation-vs-replacement negatives).
Anchors located 20/11: `extraction.py:193` (EntityExtractionService),
`domain/models.py:21` (SINGLE_VALUED_PREDICATES); line-level validity claim still
a reading — re-prove at build.

## W-7 — Index bookkeeping competes for the context budget  (LOW/MEDIUM)

The atlas flags indexing the engine's own bookkeeping: a summary memory listing
merged ids, or a review marker at confidence 1.0, spends the character budget
that facts need.

**Candidate work:** keep maintenance artefacts out of the searchable set.

**Disposition 20/11:** COULD — decision record C1. Captured, not scheduled.
Measure via the context-assembly probe before/after; no assertion-only wins.

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

**Disposition 20/11:** MUST — plan M5. Commit evidence/manifest + hashes +
recompute script, version-label every number, audit the harness for the
"ideal DCG from top-k" inflation (MemPalace signal 02/11), write the
METHODOLOGY/RESULTS docs prescribed by the 22/10 benchmark decision. The fresh
paid contract re-run rides with the S1 gate (0.3.13 default behaviour is
byte-identical to the measured 0.3.11 behaviour with switches off).
**Built 20/11 (M5):** `METHODOLOGY.md`, `RESULTS.md`, archive file manifest,
offline recompute script and corrected `_about`; archive `SHA256SUMS` verified,
all three headline scores and paired ranges reproduced. Per-question outputs
were not redistributed because dataset terms are not established. Local commit
`c6c02c2`. Paid re-run still rides with S1.

## W-9 — Missing mechanisms the rubric looked for  (assess)

Not found at this commit: `tombstone` (see W-1 — `:erased` is the near-miss),
`trust_state`, `human_review`. There is no approval/review queue surface anywhere
in `src/` or `web/src`. `TruthState` is computed at read time and never stored
(`models.py:24-35`) — which is fine, but means nothing carries candidate/
verified/rejected.

**Candidate work:** decide whether any of the three is wanted. If not, say so in
the README so the absence is a decision rather than an omission.

**Disposition 20/11:** split. MUST — plan M4: record the absences as deliberate
decisions (tombstone near-miss resolved by M1; human_review WON'T per N7 — no
demonstrated need, governance already approval-gated). COULD — stored trust
tiers captured as W-18 (C6), only if S2 curation or W-13 promotion needs them.
**Built 20/11 (M4):** README "Deliberate absences" records read-time trust state,
no human-review queue and M1 tombstones as decisions; local commit `021e627`.
Stored trust tiers remain W-18 (Could).

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

**Disposition 20/11:** MUST — plan M7 (RED tests woven into M1–M3; W-2 test
waived by owner decision, documented). Both backends + live-PG lane.

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

**Disposition 20/11:** MUST — plan M7: pin all five with named tests.

## W-12 — Reviewer's own limits  (context, not work)

The atlas page says explicitly that the marks are one reviewer's reading of the
code, not a run, and that a dash means "not found", not "missing". Treat every
item above as a lead to verify, not a verdict. Two build-time execution paths were
noted and not triggered.

**Disposition 20/11:** context. Kensei re-proved W-1, W-3 (four paths), W-4, W-5
in code on 20/11 (decision record §2 verification ledger); remaining readings
re-proved at build per plan ground rule 1.

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

**Status (20/11):** MUST — plan M6, approved. Owner proceeded on the
recommendations, so the two decisions are TAKEN AS the recommended options:
D1 = A (signal = user-stated `user_fact` memories + corrected/revised facts, not
a confidence threshold) and D2 = A (default-profile store only: tenant `kensei`,
profile `default`, collection `profile`, agent `default`, session `shared`).
Override window open at plan review (plan Q3). Separate agent lane; handoff
written after plan approval.

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

## W-14 — Deterministic human-readable export (OKF-style)  (COULD)

**Source:** decision record C2; upstream signal 16/11/26 — Hindsight Observation
Tree export as Open Knowledge Format v0.2 (`vectorize-io/hindsight` #6519, MIT,
deterministic no-LLM markdown bundle, tag-scoped, stability 100% reordered /
84–97% under removals). Design-only reimplementation — no code port.

**What:** a canonical, browseable/grep-able markdown export of a scope's
observations and memories, deterministic ordering, no model in the export path.
Pairs with M1 purge verification (export must exclude purged text) and W-13
browsability.

**Acceptance sketch:** export is byte-stable under reordering of input rows;
scope-tagged; purged/erased text absent; round-trip manifest hashes verify.

**Status:** could — captured 20/11, NOT scheduled. Revisit after M1 lands.

## W-15 — Idempotent repair walk  (COULD)

**Source:** decision record C3; upstream signal 28/09/26 — MemPalace guided
audit + repair tooling (`mempalace/mempalace` #2576, MIT). The only EXTRACT
verdict in 8 weeks of signal reports.

**What:** read-only audit walk that reports canonical-vs-index drift, plus a
reversible `--repair` that converges with zero loss (journaled, rollback-safe).
Severian already has read-only audit + projection rebuild controls; this wraps
them into an operator walk.

**Acceptance sketch:** full drift report on synthetic fixtures; two runs converge
with zero loss; repair is journaled and reversible; no live-store writes without
explicit approval.

**Trigger:** build before the first big migration (S1) or the first drift
incident — not before. Live stores are healthy today.

**Status:** could — captured 20/11, NOT scheduled.

## W-16 — Upstream-failure conformance test pack  (COULD)

**Source:** decision record C4; upstream signals 12/10–16/11/26. Cheap test
additions, no behaviour change. Items adjacent to M1/M2 fold into the Must wave;
the remainder live here:

- update never re-creates a deleted record (mem0 #7787/#7788, 02/11);
- concurrent retains of identical content store each fact exactly once
  (Hindsight #6433, 09/11);
- derived counters are transactionally serialised or rebuild-from-canonical
  (Hindsight #6233, 02/11);
- racing restore refuses instead of silently merging; backup archives stay under
  the tenant's storage prefix (Hindsight #6408/#6409, 09/11);
- no secrets at any log level, including error paths (mem0 12/10, MemPalace
  3.12.0 19/10);
- entry must bind to its owning scope; stale binding refused like a forbidden id
  (cognee, 19/10).

**Acceptance sketch:** each named failure mode has a test that fails on a
deliberately broken variant and passes on current code (or a fix is filed).

**Status:** could — captured 20/11, NOT scheduled.

## W-17 — Dashboard UI/UX rescope  (COULD)

**Source:** decision record C5; ops board card `t_97fb7ecf` (backlog): "Revisit
Severian memory-system dashboard UI/UX".

**What:** the card is STALE — it points at the old `ultramemory-dashboard` repo
and naming, and a dev backend at 127.0.0.1:8000. Rescope against the current
`web/` surface in this repo first (design consistency, interaction polish,
accessibility, graph-view performance, empty/error states). Dezzy lane; separate
track from the engine iteration. Owner decision D4 (rescope yes/no) still pending.

**Status:** could — captured 20/11, NOT scheduled. Do not dispatch the stale card
as-is.

## W-18 — Stored trust tiers (candidate/verified/rejected)  (COULD)

**Source:** decision record C6; W-9 partial. `TruthState` is computed at read
time and never stored (`domain/models.py:24-35`).

**What:** persisted trust tiers ONLY if S2 curation or W-13 promotion needs a
stored trust signal. Otherwise the read-time design stands and the absence is a
recorded decision (M4). Do not build a trust layer speculatively.

**Status:** could — captured 20/11, NOT scheduled. Revisit when S2/M6 evidence
shows a need.

## W-19 — Snapshot import rejects real stores (M0; REV MG-1)  (HIGH)

**Source:** objective review 20/11 (REV MG-1), probe-confirmed; README l.89
advertises export/import. Built FIRST in the Must wave (plan v2 M0).

**What was broken:** `import_json` (both backends share `validate_snapshot`)
rejected any store the engine produces through its normal paths. Seven distinct
causes, all re-proved in code and pinned by tests:
1. derive-path relations keyed `("relation", s, p, o)`; the validator only knew
   the `add_relation` scheme `(s, p, o, valid_from, valid_to)`;
2. `task_note` missing from `RECORD_MODELS`;
3. dated manual relations: the writer hashes datetime objects (`default=str`,
   space form), the validator hashed the payload ISO strings (T form);
4. retired task notes: `_retire` writes `replaced_at` via `isoformat()`
   (+00:00) while pydantic dumps Z — strict round-trip byte-equality rejected;
5. erasure tombstones carry `superseded_by_id = ERASED` (a terminal marker, not
   a reference) — the topology check rejected;
6. correction propagation sets `superseded_by_id = "source-rev:<obs id>"` (a
   synthetic marker) — the topology check rejected;
7. closed memories: `close_memory` writes `valid_until` via `isoformat()`
   (+00:00) vs pydantic Z — same round-trip class as (4).

**Fix (import path only):** `snapshot.py` accepts both relation writer schemes
(each recomputed exactly), adds `task_note` (status-aware identity, strict in
both directions), tolerates equivalent ISO datetime instants in the round-trip
check (no other field tolerated), and treats ERASED / source-rev markers as
terminal (the source-rev suffix must name an existing same-scope observation).
No write-path change; no migration; existing rows unchanged.

**Status:** BUILT 20/11 on branch `fix/must-integrity-2026-11-20` (M0 commit;
exact hash recorded in the evidence file and ticked here at wave close-out).
RED-first on SQLite and live PG; forgery pins added. Wave gate (§3.9) pending.
Evidence: `~/docs/severian-programme/evidence/must-should-2026-11-20/M0.md`.

---

### Triage table (updated 20/11/26 — owner direction: Must + Should proceed to
### objective review; Could captured, not scheduled; Won't stands)

| id | item | severity | disposition 20/11 | plan ref |
|----|------|----------|-------------------|----------|
| W-1 | erasure keeps text | high | MUST M1 — BUILT + independent review PASS; WAVE GATE PASS at ec719e8 (M1 8a17a9f) | M1 / REV A1, MG-2, MG-3, MG-7 |
| W-2 | second-order correction | high | WON'T — owner decision 14/11 (t_54fb0317), documented; reopen on owner call | N1 |
| W-3 | five unaudited paths (SIX after REV A2; `record_memory_usage` exempt) | high | MUST M2 — BUILT; WAVE GATE PASS at ec719e8 (921ce62) | M2 / REV A2, MG-4 |
| W-4 | dream jobs have no producer | medium | MUST docs now + SHOULD producer — M4 docs DONE 20/11 (021e627); S2 producer open | M4 + S2 |
| W-5 | MCP scope chosen by model | medium | MUST M3 — BUILT + controller gate PASS; WAVE GATE PASS at ec719e8 (be12e08) | M3 |
| W-6 | relation validity is record time | medium | SHOULD | S3 |
| W-7 | bookkeeping in the index | low | COULD — captured, not scheduled | C1 |
| W-8 | benchmark evidence not reproducible | medium | MUST M5 — BUILT (c6c02c2), recompute PASS, temp fix 0046237; paid re-run rides S1 | M5 |
| W-9 | missing mechanisms | assess | MUST docs decision DONE 20/11 (021e627); COULD trust tiers W-18 | M4 + W-18 |
| W-10 | tests for all of the above | high | MUST M7 — DONE 20/11: transaction/scope tests added; five mutation witnesses recorded; SQLite 1638/120, PG 1738/20; tests commit 14a667d | M7 |
| W-11 | strengths to keep proven | n/a | MUST M7 — DONE 20/11: five strengths mapped and each mutation-witnessed; see M7.md | M7 |
| W-12 | reviewer limits | n/a | context — ledger in decision record §2 | — |
| W-13 | rebuild memory→wiki promotion on Severian | medium | MUST — approved; D1/D2 taken as A/A (override open); separate agent lane | M6 |
| W-14 | deterministic human-readable export | low | COULD — captured 20/11, not scheduled | C2 |
| W-15 | idempotent repair walk | low | COULD — captured 20/11, trigger: before S1 migration or first drift | C3 |
| W-16 | upstream-failure conformance tests | low | COULD — captured 20/11, not scheduled | C4 |
| W-17 | dashboard UI/UX rescope | low | COULD — captured 20/11, stale card needs rescope; D4 pending | C5 |
| W-18 | stored trust tiers | low | COULD — captured 20/11, only on S2/M6 demonstrated need | C6 |
| W-19 | snapshot import rejects real stores | high | MUST M0 — BUILT (5c36950); WAVE GATE PASS at ec719e8 | M0 / REV MG-1 |
