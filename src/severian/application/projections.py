from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.vector import VectorHit


@runtime_checkable
class VectorProjection(Protocol):
    """Duck-typed vector projection surface (LocalVectorIndex or PGVectorIndex)."""

    def upsert(self, memory: Memory) -> None: ...
    def delete(self, memory_id: str) -> None: ...
    def count(self, scope: Scope) -> int: ...
    def clear(self, scope: Scope | None = None) -> None: ...
    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[VectorHit]: ...
    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ProjectionStatus:
    canonical_memories: int
    lexical_entries: int
    vector_entries: int

    @property
    def in_sync(self) -> bool:
        return self.canonical_memories == self.lexical_entries == self.vector_entries


class ProjectionCoordinator:
    def __init__(self, repository: CanonicalRepository, lexical: FTS5Index, vector: VectorProjection):
        self.repository = repository
        self.lexical = lexical
        self.vector = vector

    def index_memory(self, memory: Memory) -> None:
        self.lexical.upsert(memory)
        self.vector.upsert(memory)

    def remove_memory(self, memory_id: str) -> None:
        self.lexical.delete(memory_id)
        self.vector.delete(memory_id)

    def rebuild(self, scope: Scope) -> None:
        self.lexical.rebuild(self.repository, scope)
        self.vector.rebuild(self.repository, scope)

    def status(self, scope: Scope) -> ProjectionStatus:
        canonical = len(self.repository.list_memories(scope))
        return ProjectionStatus(
            canonical_memories=canonical,
            lexical_entries=self.lexical.count(scope),
            vector_entries=self.vector.count(scope),
        )
