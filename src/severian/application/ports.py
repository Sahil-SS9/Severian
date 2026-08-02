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

    def add_observation(self, observation: Observation) -> Observation: ...
    def add_observation_and_enqueue(
        self, observation: Observation, job_kind: str = "derive"
    ) -> tuple[Observation, Job]: ...
    def get_observation(self, scope: Scope, observation_id: str) -> Observation: ...
    def list_observations(self, scope: Scope) -> list[Observation]: ...
    def revise_observation(self, scope: Scope, observation_id: str, content: str) -> Observation: ...

    def add_memory(self, memory: Memory) -> Memory: ...
    def add_memories_batch(self, memories: list[Memory]) -> list[Memory]: ...
    def get_memory(self, scope: Scope, memory_id: str) -> Memory: ...
    def list_memories(self, scope: Scope) -> list[Memory]: ...
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


@runtime_checkable
class OutboxPort(Protocol):
    """Append-only event outbox produced by canonical writes.

    P0-4 contract: consumers ack events after their projection work
    converges; ``pending_outbox`` returns only unacked events so a
    crash mid-consumption is retried on restart.
    """

    def append_outbox(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Event: ...
    def pending_outbox(self) -> list[Event]: ...
    def ack_outbox(self, event_id: str) -> None: ...


@runtime_checkable
class SnapshotAdminPort(Protocol):
    """Administrative export/import/backup. Destructive, cross-scope."""

    def export_json(self) -> str: ...
    def import_json(self, payload: str) -> None: ...
    def backup(self, destination: Path) -> None: ...


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
