"""Backend-neutral canonical repository contracts.

The contracts here define the shape a backend must satisfy to be used
with the Severian memory engine. The on-disk :class:`SQLiteRepository`
is the reference implementation and the source of truth for the
contract surface; :class:`~severian.infrastructure.postgresql.PostgreSQLBackend`
is the live PostgreSQL/pgvector backend and satisfies the same
:class:`CanonicalRepository` protocol (proven by the parametrized
conformance suite in ``tests/contract/``).

Rules enforced by the contracts:

- SQLite/WAL canonical records remain authoritative. FTS5 and local
  vector indexes are separate, derived, disposable, rebuildable
  projections that cannot become canonical truth.
- Projections are rebuildable; the canonical store must not depend
  on them for correctness.
- Immutable source observations remain separate from derived memories.
- Scope (tenant, profile, collection, agent, session) is a mandatory
  security boundary applied to direct reads, provenance, entities,
  relations, projections, retrieval and context assembly.
- Scope dimensions must not be blank.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from severian.domain.models import (
    Collection,
    Entity,
    Event,
    Job,
    Memory,
    Observation,
    Relation,
    Scope,
)
from severian.infrastructure.sqlite import NotFoundError

# The contract's NotFoundError is the same class raised by the reference
# ``SQLiteRepository`` so that callers catching the contract error also
# catch the backend's error. The canonical store must never reveal that a
# resource exists in another scope; this error is the only signal a caller
# receives for cross-scope lookups.
NotFoundError = NotFoundError


@runtime_checkable
class CanonicalRepository(Protocol):
    """Canonical storage contract for the Severian memory store.

    This protocol mirrors the surface of ``SQLiteRepository`` exactly so
    that the reference backend is itself a conformant implementation and
    the conformance suite is not vacuous.

    Scope semantics:
    - Every scoped read returns only resources belonging to the
      requested scope. Cross-scope reads raise ``NotFoundError``.
    - Writes are rejected if the scope dimensions do not match the
      resource's stored scope.
    - Blank scope dimensions are rejected before storage access.
    """

    # -- lifecycle ---------------------------------------------------------

    def initialize(self) -> None: ...
    def close(self) -> None: ...

    # -- collections -------------------------------------------------------

    def create_collection(self, collection: Collection) -> Collection: ...
    def get_collection(self, tenant_id: str, collection_id: str) -> Collection: ...

    # -- observations ------------------------------------------------------

    def add_observation(self, observation: Observation) -> Observation: ...
    def get_observation(self, scope: Scope, observation_id: str) -> Observation: ...
    def list_observations(self, scope: Scope) -> list[Observation]: ...

    # -- memories ----------------------------------------------------------

    def add_memory(self, memory: Memory) -> Memory: ...
    def get_memory(self, scope: Scope, memory_id: str) -> Memory: ...
    def list_memories(self, scope: Scope) -> list[Memory]: ...

    # -- derivation jobs ---------------------------------------------------

    def enqueue_job(self, scope: Scope, kind: str, payload: dict[str, Any]) -> Job: ...
    def claim_job(self, worker_id: str) -> Job: ...
    def complete_job(self, job_id: str, status: str) -> Job: ...

    # -- entities and relations --------------------------------------------

    def add_entity(self, scope: Scope, kind: str, name: str) -> Entity: ...
    def get_entity(self, scope: Scope, entity_id: str) -> Entity: ...
    def add_relation(
        self,
        scope: Scope,
        subject_id: str,
        predicate: str,
        object_id: str,
        *,
        valid_from: Any | None = None,
        valid_to: Any | None = None,
    ) -> Relation: ...

    # -- audit and recovery ------------------------------------------------

    def append_audit(self, scope: Scope, action: str, target_id: str, metadata: Mapping[str, Any]) -> Any: ...
    def append_outbox(self, scope: Scope, kind: str, payload: Mapping[str, Any]) -> Event: ...
    def pending_outbox(self) -> list[Event]: ...
    def export_json(self) -> str: ...
    def import_json(self, payload: str) -> None: ...
    def backup(self, destination: Any) -> None: ...


@runtime_checkable
class ProjectionIndex(Protocol):
    """Derived, rebuildable projection contract.

    Projections (FTS, vector, entity indexes) are rebuilt from
    canonical records. They are disposable: a rebuild must produce
    the same results given the same canonical data and deterministic
    derivation. The canonical store must not depend on a projection
    for correctness.

    Scope semantics match the canonical repository: every scoped read
    returns only resources belonging to the requested scope.
    """

    def index(self, scope: Scope, resource_id: str, content: str) -> None: ...
    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[ProjectionHit]: ...
    def delete(self, scope: Scope, resource_id: str) -> None: ...
    def clear(self, scope: Scope) -> None: ...
    def count(self, scope: Scope) -> int: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ProjectionHit:
    """A single hit from a projection index search."""

    resource_id: str
    score: float
    scope: Scope


# -- scope safety ----------------------------------------------------------

def check_scope(scope: Scope) -> Scope:
    """Validate that scope dimensions are present and non-blank.

    Raises ``ValueError`` on blank dimensions so that callers can
    fail closed before reaching storage.
    """
    if not scope.tenant_id.strip():
        raise ValueError("scope dimension tenant_id must not be blank")
    if not scope.profile_id.strip():
        raise ValueError("scope dimension profile_id must not be blank")
    if not scope.collection_id.strip():
        raise ValueError("scope dimension collection_id must not be blank")
    if not scope.agent_id.strip():
        raise ValueError("scope dimension agent_id must not be blank")
    if not scope.session_id.strip():
        raise ValueError("scope dimension session_id must not be blank")
    return scope


__all__ = [
    "CanonicalRepository",
    "check_scope",
    "NotFoundError",
    "ProjectionHit",
    "ProjectionIndex",
]
