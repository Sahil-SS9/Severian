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

from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

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
)


@runtime_checkable
class ScopedCanonicalPort(Protocol):
    """Scoped canonical reads/writes available to normal user flows."""

    def create_collection(self, collection: Collection) -> Collection: ...
    def get_collection(self, tenant_id: str, collection_id: str) -> Collection: ...
    def collection_exists(self, collection_id: str) -> bool: ...

    def add_observation(self, observation: Observation) -> Observation: ...
    def add_observation_and_enqueue(
        self, observation: Observation, job_kind: str = "derive"
    ) -> tuple[Observation, Job]: ...
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
    ) -> Observation:
        """Supersede an observation with a revision, in one transaction.

        ``derive=True`` also enqueues the revision's derive job, so a
        correction is re-derived on the next drain instead of vanishing.
        ``tombstone=True`` creates an erasure marker that is itself
        superseded (``superseded_by_id=":erased"``): never active, never
        pending derivation, kept in history for audit. The two are
        mutually exclusive.
        """
        ...

    def add_memory(self, memory: Memory) -> Memory: ...
    def add_memories_batch(self, memories: list[Memory]) -> list[Memory]: ...
    def publish_derivation(
        self,
        scope: Scope,
        *,
        entities: list[tuple[str, str]],
        relations: list[tuple[str, str, str]],
        memories: list[Memory],
        source_observation_id: str | None = None,
    ) -> list[Memory]:
        """Atomically publish graph extraction + derived memories.

        ``source_observation_id`` is recorded as relation lineage: a new
        relation starts with it; an existing identical relation gains it
        (and is reinstated if it had been retracted).
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
    def get_memory(self, scope: Scope, memory_id: str) -> Memory: ...
    def count_memories(self, scope: Scope) -> int: ...
    def list_memories(self, scope: Scope) -> list[Memory]: ...
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
    def supersede_memory(self, scope: Scope, memory_id: str, replacement_id: str) -> Memory:
        """Mark one active memory as superseded by a replacement head.

        Generic form of ``supersede_memories_for_source`` used by the
        dreaming/consolidation pass: the replaced memory stays in history
        (P0-1), but default reads and projection rebuilds drop it. Returns
        the superseded memory (now marked).
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
    def claim_job(self, worker_id: str) -> Job: ...
    def complete_job(
        self,
        job_id: str,
        status: str,
        *,
        worker_id: str | None = None,
        error: str | None = None,
    ) -> Job: ...
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
