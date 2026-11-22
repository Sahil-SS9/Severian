"""Application-owned backend contracts — the single source of truth.

Every adapter (API, CLI, SDK, MCP, Hermes) and every application service
depends on the protocols defined here. Concrete backends
(``SQLiteRepository``, ``PostgreSQLBackend``) implement them; the
conformance suite asserts runtime ``isinstance`` satisfaction so the
contract is never vacuous.

The contract is deliberately split into narrow ports so that callers
only see the surface they are allowed to touch:

- :class:`ScopedCanonicalPort` — scoped reads/writes a normal user flow
  may perform.
- :class:`JobQueuePort` — durable derivation job queue consumed by the
  worker (trusted runtime component).
- :class:`OutboxPort` — append-only event outbox (produced by writes,
  consumed by projection consumers).
- :class:`SnapshotAdminPort` — export/import/backup (administrative
  tooling only; destructive and cross-scope).
- :class:`CanonicalRepository` — the union of all ports, implemented by
  every concrete backend.

Scope rules (enforced by the backends and asserted by the conformance
suite):

- Every scoped read returns only resources belonging to the requested
  scope; cross-scope reads raise ``NotFoundError`` and must not reveal
  whether the resource exists elsewhere.
- Writes reject scope mismatches with ``ConflictError``.
- Blank scope dimensions are rejected before storage access.

Errors are shared classes in ``severian.domain.errors`` so callers
catch one type regardless of backend.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from severian.application.purge import EraseOutcome, PurgePlan, PurgeReport
from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import (
    AuditEvent,
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
    Scope,
    TaskNote,
)


@runtime_checkable
class ScopedCanonicalPort(Protocol):
    """Scoped canonical reads/writes available to normal user flows."""

    def create_collection(self, collection: Collection) -> Collection: ...
    def get_collection(self, tenant_id: str, collection_id: str) -> Collection: ...
    def collection_exists(self, collection_id: str) -> bool: ...

    def add_observation(self, observation: Observation) -> Observation: ...
    def add_observation_and_enqueue(
        self, observation: Observation, job_kind: str = "derive", *, also_queue: Sequence[str] = ()
    ) -> tuple[Observation, Job]:
        """Insert an observation and queue its ``job_kind`` job, plus one job per ``also_queue`` kind,
        in ONE transaction (P0-4). A re-ingest returns the stored pair and queues missing extra jobs."""
        ...
    def add_observations_and_enqueue_batch(
        self, observations: list[Observation], job_kind: str = "derive"
    ) -> list[tuple[Observation, Job]]:
        """Batch observation ingest in ONE transaction (zero-debt 3.3).

        Same canonical contract as ``add_observation_and_enqueue``.
        The default loops the single method so any implementer that
        does not override stays correct; real backends override with a
        single-transaction batch.
        """
        return [self.add_observation_and_enqueue(o, job_kind) for o in observations]
    def get_observation(self, scope: Scope, observation_id: str) -> Observation: ...
    def list_observations(self, scope: Scope) -> list[Observation]: ...
    def list_pending_observations(self, scope: Scope, *, limit: int) -> list[Observation]:
        """Active observations with NO derived active memory yet.

        O(1)/indexed pending-work lookup for the synchronous derive
        path (zero-debt 3.1): derive must not scan every historical
        observation nor count the whole scope per observation. The
        store answers with a bounded SQL query; observations that
        already produced a memory are excluded server-side.
        """
        ...

    def list_observations_history(self, scope: Scope) -> list[Observation]:
        """All observations in scope, INCLUDING superseded revisions.

        The canonical store retains every observation as immutable raw
        evidence (P0-1): a revision marks the old head superseded but
        never rewrites it. History powers temporal/rollback queries
        (B-1 temporal probes: "what was the OLD fact before the
        correction?") where the active-only view would hide the answer.
        """
        ...
    def revise_observation(
        self,
        scope: Scope,
        observation_id: str,
        content: str,
        *,
        derive: bool = False,
        tombstone: bool = False,
        also_queue: Sequence[str] = (),
    ) -> Observation:
        """Supersede an observation with a revision, in one transaction.

        ``derive=True`` also enqueues the revision's derive job, so a
        correction is re-derived on the next drain instead of vanishing.
        ``tombstone=True`` creates an erasure marker that is itself
        superseded (``superseded_by_id=":erased"``): never active, never
        pending derivation, kept in history for audit. The two are
        mutually exclusive. ``also_queue`` adds one job per kind for the
        revision in the same transaction (never with ``tombstone``).
        """
        ...

    def add_memory(self, memory: Memory) -> Memory: ...
    def add_memory_with_audit(
        self,
        memory: Memory,
        *,
        action: str,
        metadata_: dict[str, Any],
    ) -> tuple[Memory, AuditEvent]:
        """Create ``memory`` AND one audit row for it, in a single transaction.

        Creation is ``add_memory``'s and is unchanged: same identity key, same
        dedupe against an identical memory in the scope, same ``memory.created``
        outbox event, so the projection still sees exactly one creation. The one
        addition is an append-only audit row — ``action`` against the stored
        memory's id, carrying ``metadata_`` (ids, counts and times, never
        content) — written in the SAME transaction, so neither write can land
        without the other.

        Use this instead of ``add_memory`` followed by ``append_audit``: two
        calls are two transactions, and a failure between them leaves a claim
        with no evidence. On an identity hit nothing is inserted, and the row
        names the memory that is actually stored.
        """
        ...
    def add_memories_batch(self, memories: list[Memory]) -> list[Memory]: ...
    def publish_derivation(
        self,
        scope: Scope,
        *,
        entities: list[tuple[str, str]],
        relations: list[tuple[str, str, str]],
        memories: list[Memory],
        source_observation_id: str | None = None,
        relation_valid_from: datetime | None = None,
    ) -> list[Memory]:
        """Atomically publish graph extraction + derived memories.

        ``source_observation_id`` is recorded as relation lineage: a new
        relation starts with it; an existing identical relation gains it
        (and is reinstated if it had been retracted).

        ``relation_valid_from`` is the instant a relation created in this call
        starts — the source message's event time when
        ``SEVERIAN_RELATION_EVENT_TIME`` is on. The caller decides it; a backend
        only stores it, and relation identity stays free of validity. The same
        instant is where an older value of a single-valued predicate ends, and
        it never ends a value that began later (S3). ``None`` leaves the old
        behaviour untouched: no start time, and ends stamped at the wall clock.
        """
        ...
    def retract_relations_for_source(self, scope: Scope, observation_id: str) -> int:
        """Drop ``observation_id`` from every relation's lineage.

        A relation left with no supporting observation is retracted
        (``retracted_at`` set) and leaves the current graph; history keeps
        it. Relations without lineage (added manually) are untouched.
        Returns the number of relations retracted.
        """
        ...

    def erase_observation_cascade(
        self, scope: Scope, observation_id: str, *, reason: str = "erased"
    ) -> EraseOutcome:
        """Erase one observation and everything derived from it in ONE transaction (M1, MG-3).

        The whole cascade the old service path ran across four transactions: the erasure
        tombstone for the observation, one tombstone per ACTIVE memory derived from it, and
        the retraction of the relations that lose it as their last support. Because it is
        one transaction, a crash can never leave a derived memory active after the source
        that produced it was erased. Outbox and audit rows are the ones the old path wrote
        (``observation.revised``, each tombstone's ``memory.created``, ``memory.supersede``,
        ``relation.unsupport``/``relation.retract``) plus one ``observation.erase`` summary.

        Raises ``ConflictError`` when the observation is already superseded (a second
        erasure changes nothing) and ``NotFoundError`` when it is not in this scope.
        """
        ...

    def plan_purge(self, scope: Scope, observation_id: str) -> PurgePlan:
        """Read-only dry run: every record id a purge of ``observation_id`` would scrub.

        One scope is loaded and the shared planner decides; nothing is written. Ids and
        counts only, never content. ``NotFoundError`` when the id is not in this scope.
        """
        ...

    def purge_observation(self, scope: Scope, observation_id: str) -> PurgeReport:
        """Remove an observation's text from the store for good, in ONE transaction (M1).

        Erases the observation if it is still active and scrubs, in the same transaction:
        the whole observation revision family (root → revisions → erasure tombstone), every
        memory derived from it (revisions and tombstones included), the relations that cited
        it (lineage shortened; retracted when nothing supports them any more) and the entity
        names nothing else supports. Every scrubbed row carries ``PURGE_MARKER``, digests
        recomputed over that marker, released identity keys, and is never an active head;
        observations lose their ``source``/``attachments`` and memories their cues/trigger.

        Pending derivation for the chain is cancelled in the same transaction (a queued job
        must not re-derive from the marker); a RUNNING job for the chain refuses the purge
        with ``ConflictError`` ("stop the worker first"), matching ``cancel_pending_jobs``.

        The projection rebuild happens in the service after this commits (projections are
        derived and converge). Audit rows ``observation.purge`` / ``memory.purge`` /
        ``relation.purge`` / ``entity.purge`` are written per target id, with ids and counts
        and no content, through the audit-only helper.

        This is not physical erasure of every copy: backups and snapshots outside the store
        are unreachable from here (see ``docs/operator-runbook.md``).
        """
        ...
    def get_memory(self, scope: Scope, memory_id: str) -> Memory: ...
    def count_memories(self, scope: Scope) -> int: ...
    def list_memories(self, scope: Scope) -> list[Memory]: ...
    def list_session_observations(self, scope: Scope, session_ids: list[str]) -> list[Observation]:
        """Observations in scope from the given conversations (``source.session_id``)."""
        ...

    def list_memories_for_observations(self, scope: Scope, observation_ids: list[str]) -> list[Memory]:
        """Active memories in scope derived from any of the given observations."""
        ...

    def list_trigger_memories(self, scope: Scope) -> list[Memory]:
        """ACTIVE memories that carry a trigger (instructions for later), oldest first."""
        ...
    def list_memories_history(self, scope: Scope) -> list[Memory]: ...
    def list_memory_revisions(self, scope: Scope, memory_id: str) -> list[Memory]:
        """Every revision in the memory's lineage, oldest first.

        The lineage is the whole ``revision_of_id`` family, so a head
        demoted by rollback is still listed.
        """
        ...
    def record_memory_usage(self, scope: Scope, memory_id: str) -> Memory:
        """Increment a memory's usage_count by one (retrieval hit).

        Tier 2 salience: feeds the curation salience blend and the
        optional retrieval decay. Deterministic and idempotent per call.
        """
        ...

    def rollback_memory(self, scope: Scope, revision_id: str) -> Memory:
        """Restore a historical revision as the active memory head.

        ChronoMem-style rollback (roadmap A-2): reverses a bad
        correction by making ``revision_id`` the current head again.
        The current head is superseded by the restored revision.
        Returns the restored (now-active) memory.
        """
        ...
    def supersede_memories_for_source(self, scope: Scope, observation_id: str) -> int: ...
    def close_memory(self, scope: Scope, memory_id: str, *, valid_until: datetime, replaced_by_id: str) -> bool:
        """Mark one ACTIVE, still-open memory as no longer current (a newer fact replaced it).

        Sets ``valid_until`` and ``replaced_by_id`` on the stored record in one atomic update and
        returns True. The memory is NOT superseded, deleted or removed from any index: it stays
        readable as history. Returns False (and changes nothing) if the memory is not in this scope,
        is superseded or erased, or is already closed — so repeating the call is harmless.
        """
        ...
    def supersede_memory(self, scope: Scope, memory_id: str, replacement_id: str) -> Memory:
        """Mark one active memory as superseded by a replacement head.

        Generic form of ``supersede_memories_for_source`` used by the
        dreaming/consolidation pass: the replaced memory stays in history
        (P0-1), but default reads and projection rebuilds drop it. Returns
        the superseded memory (now marked).

        Implemented as ONE conditional update guarded on the active state and
        the full scope, so a concurrent close, erase or supersede is never lost.
        Anything the guard rejects — an absent id, a row in another scope, an
        erased or already superseded memory — raises ``ConflictError`` and
        changes nothing. This deliberately does not distinguish "absent" from
        "already superseded": a diagnostic read would be a second, non-atomic
        source of truth and could reveal that a foreign-scope row exists.
        """
        ...
    def revise_memory(
        self,
        scope: Scope,
        memory_id: str,
        *,
        content: str,
        model_id: str,
        prompt_version: str,
    ) -> Memory: ...

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity: ...
    def get_entity(self, scope: Scope, entity_id: str) -> Entity: ...
    def list_entities(self, scope: Scope) -> list[Entity]:
        """All entities in scope (active; entities are not superseded)."""
        ...
    def get_relation(self, scope: Scope, relation_id: str) -> Relation: ...
    def list_relations(self, scope: Scope) -> list[Relation]:
        """All relations in scope, regardless of temporal validity.

        Graph traversal filters validity itself (a relation with a closed
        valid_to window is historical, not current).
        """
        ...
    def add_relation(
        self,
        scope: Scope,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> Relation: ...

    def append_audit(
        self,
        scope: Scope,
        action: str,
        target_id: str,
        metadata_: dict[str, Any],
    ) -> AuditEvent: ...
    def list_audit(self, scope: Scope) -> list[AuditEvent]: ...


@runtime_checkable
class JobQueuePort(Protocol):
    """Durable derivation job queue consumed by trusted workers.

    P0-4 contract: claims are leased (``lease_until`` + worker id),
    completion is owner-checked, attempts/backoff/dead-letter are
    first-class, and expired leases can be reaped for retry.
    """

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job: ...
    def claim_job(
        self,
        worker_id: str,
        lease_seconds: int = 300,
        *,
        kind: str | None = None,
        exclude_kinds: Sequence[str] = (),
    ) -> Job: ...
    def complete_job(
        self,
        job_id: str,
        status: str,
        *,
        worker_id: str | None = None,
        error: str | None = None,
        retry_in: float | None = None,
    ) -> Job:
        """``status`` is ``done``, ``failed`` or ``dead_letter`` (0.6.3: kept apart for ``requeue_job``
        and the drain alarm). ``retry_in`` (failed only): put the job back to ``pending``, claimable
        after that many seconds."""
        ...
    def release_job(self, job_id: str, *, worker_id: str | None = None) -> Job:
        """Give a claimed job back (``running`` -> ``pending``) WITHOUT using up an attempt (0.6.3).

        For work never attempted, e.g. the model server was found down before the call; the error
        and retry time are left as they were. Owner-checked like ``complete_job``.
        """
        ...
    def list_jobs(self, *, status: str, kind: str | None = None) -> list[Job]:
        """Jobs in one state (and of one kind), oldest first: what recovery plans from."""
        ...
    def requeue_jobs(self, job_ids: Sequence[str], *, from_status: str = "failed") -> list[Job]:
        """Put ``failed`` or ``dead_letter`` jobs back to ``pending`` with a fresh attempt budget (0.6.3).

        ONE transaction. Ids not in ``from_status`` are skipped (idempotent). Each requeued job gets
        one ``job.requeue`` audit row through the audit-only helper: kind, from-state and attempts;
        never the error text, which can quote a reply. Returns the requeued jobs.
        """
        ...
    def list_profile_scopes(self, tenant_id: str, profile_id: str) -> list[Scope]:
        """Every scope holding turns, memories or jobs for one profile (for per-profile operations)."""
        ...
    def cancel_pending_jobs(self, scope: Scope, kind: str) -> int:
        """Cancel ``scope``'s pending ``kind`` jobs; ``ConflictError`` while one is running."""
        ...
    def reap_expired_jobs(self, *, now: datetime | None = None, max_attempts: int = 3) -> int: ...
    def list_dead_letter_jobs(self) -> list[Job]: ...
    def requeue_job(self, job_id: str) -> Job: ...


@runtime_checkable
class OutboxPort(Protocol):
    """Append-only event outbox produced by canonical writes.

    P0-4 contract: consumers ack events after their projection work
    converges; ``pending_outbox`` returns only unacked events so a
    crash mid-consumption is retried on restart.
    """

    def append_outbox(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Event: ...
    def pending_outbox(self) -> list[Event]: ...
    def claim_outbox(
        self,
        worker_id: str,
        *,
        limit: int = 500,
        lease_seconds: int = 300,
    ) -> list[Event]:
        """Atomically claim up to ``limit`` pending events for one worker.

        HG follow-up (2026-08-22): fetch-and-mark in ONE transaction
        (``pending`` -> ``projecting``, lease recorded) so two concurrent
        workers can never apply the same event twice. Stale claims are
        returned to ``pending`` by ``reap_expired_jobs``.
        """
        ...
    def release_outbox(self, event_ids: list[str]) -> None:
        """Return claimed events to ``pending`` immediately (idempotent).

        Called when a claimed batch failed to converge: without this the
        events would sit ``projecting`` until lease expiry (300s stall on
        poison events). Release lets the next pass retry at once.
        """
        ...
    def ack_outbox(self, event_id: str) -> None: ...
    def ack_outbox_many(self, event_ids: list[str]) -> None:
        """Ack a batch in ONE transaction (zero-debt 3.3); idempotent."""
    def fail_outbox(self, event_ids: list[str], *, error: str, max_attempts: int) -> list[str]:
        """Count one failed projection attempt per event (0.6.4).

        Under ``max_attempts`` the event returns to ``pending``; at ``max_attempts`` it moves to
        ``failed`` (never acked) and its id is returned. Attempts and the last error live in the
        event's stored payload.
        """
        ...
    def failed_outbox(self) -> list[Event]:
        """Events whose projection kept failing (status ``failed``)."""
        ...
    def requeue_failed_outbox(self, event_ids: list[str] | None = None) -> int:
        """Return ``failed`` events (all, or the given ids) to ``pending``; returns how many."""
        ...


@runtime_checkable
class SnapshotAdminPort(Protocol):
    """Administrative export/import/backup. Destructive, cross-scope."""

    def export_json(self) -> str: ...
    def import_json(self, payload: str) -> None: ...
    def backup(self, destination: Path) -> None: ...


@runtime_checkable
class LexicalIndex(Protocol):
    """Structural lexical (FTS) projection surface (ARCH-03).

    Application code depends on this contract, never on the concrete
    ``FTS5Index`` class from ``severian.infrastructure.fts``.
    """

    def upsert(self, memory: Memory) -> None: ...
    def upsert_many(self, memories: list[Memory]) -> None:
        """Bulk upsert in ONE transaction (zero-debt 3.3).

        The default loops single upserts so any implementer that does
        not override stays correct; real indexes override with a
        transaction-bounded batch (never one commit per row).
        """
        for memory in memories:
            self.upsert(memory)
    def delete(self, memory_id: str) -> None: ...
    def count(self, scope: Scope) -> int: ...
    def clear(self, scope: Scope | None = None) -> None: ...
    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[Any]: ...
    def search_cues(self, scope: Scope, query: str, *, limit: int = 20) -> list[Any]:
        """Memories whose Step 4 cues match ``query``; indexes without a cue table return nothing."""
        return []
    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None: ...
    def digest(self, scope: Scope) -> str: ...
    def health(self) -> str: ...
    def close(self) -> None: ...

    @classmethod
    def is_searchable(cls, query: str) -> bool: ...


@runtime_checkable
class CanonicalRepository(ScopedCanonicalPort, JobQueuePort, OutboxPort, SnapshotAdminPort, Protocol):
    """Union of every canonical backend port.

    Concrete backends implement this whole surface; narrower ports give
    callers a visible slice. Runtime ``isinstance`` against this
    protocol is how the conformance suite proves a backend satisfies
    the contract.
    """


@runtime_checkable
class DerivationGateway(Protocol):
    """Trusted derivation backend.

    The gateway is the ONLY source of ``model_id`` and
    ``prompt_version`` provenance: candidates returned by
    ``derive()`` may not forge these fields (the derivation service
    overrides candidate-supplied values with the gateway's identity).
    """

    model_id: str
    prompt_version: str
    is_cloud: bool

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, Any]]: ...


__all__ = [
    "CanonicalRepository",
    "ConflictError",
    "DerivationGateway",
    "JobQueuePort",
    "NotFoundError",
    "OutboxPort",
    "ScopedCanonicalPort",
    "SnapshotAdminPort",
]


@runtime_checkable
class TaskNoteRepository(Protocol):
    """Optional capability: task notes (where an agent left off), one current note per task per scope.

    Kept off :class:`CanonicalRepository` so existing third-party backends stay conformant; the
    bundled SQLite and PostgreSQL backends implement it.
    """

    def set_task_note(self, scope: Scope, task: str, state: str, metadata: dict[str, Any]) -> TaskNote: ...
    def get_task_note(self, scope: Scope, task: str) -> tuple[TaskNote | None, list[TaskNote]]: ...
    def list_task_notes(self, scope: Scope) -> list[TaskNote]: ...
    def clear_task_note(self, scope: Scope, task: str) -> bool: ...
