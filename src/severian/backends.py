"""Backend-neutral contracts — re-exported from the application layer.

The authoritative protocol definitions live in
:mod:`severian.application.ports`; this module re-exports them (it does
NOT re-declare them) so that existing import paths keep working while
the source of truth stays single. It also defines the projection
contracts, which are derived/rebuildable indexes separate from the
canonical store.

Rules enforced by the contracts:

- SQLite/WAL canonical records remain authoritative. FTS5 and vector
  indexes are separate, derived, disposable, rebuildable projections
  that cannot become canonical truth.
- Projections are rebuildable; the canonical store must not depend on
  them for correctness.
- Immutable source observations remain separate from derived memories.
- Scope (tenant, profile, collection, agent, session) is a mandatory
  security boundary applied to direct reads, provenance, entities,
  relations, projections, retrieval and context assembly.
- Scope dimensions must not be blank.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from severian.application.ports import (
    CanonicalRepository,
    ConflictError,
    DerivationGateway,
    JobQueuePort,
    NotFoundError,
    OutboxPort,
    ScopedCanonicalPort,
    SnapshotAdminPort,
)
from severian.domain.models import Scope

__all__ = [
    "CanonicalRepository",
    "ConflictError",
    "DerivationGateway",
    "JobQueuePort",
    "NotFoundError",
    "OutboxPort",
    "ProjectionHit",
    "ProjectionIndex",
    "ScopedCanonicalPort",
    "SnapshotAdminPort",
    "check_scope",
]


@dataclass(frozen=True)
class ProjectionHit:
    """A single hit from a projection index search."""

    resource_id: str
    score: float
    scope: Scope


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
