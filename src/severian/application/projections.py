from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from severian.application.ports import CanonicalRepository, LexicalIndex
from severian.domain.hits import VectorHit
from severian.domain.models import Memory, Scope


@runtime_checkable
class VectorProjection(Protocol):
    """Duck-typed vector projection surface (LocalVectorIndex or PGVectorIndex)."""

    def upsert(self, memory: Memory) -> None: ...
    def delete(self, memory_id: str) -> None: ...
    def count(self, scope: Scope) -> int: ...
    def clear(self, scope: Scope | None = None) -> None: ...
    def search(self, scope: Scope, query: str, *, limit: int = 20) -> list[VectorHit]: ...
    def rebuild(self, repository: CanonicalRepository, scope: Scope) -> None: ...
    def digest(self, scope: Scope) -> str: ...
    def health(self) -> str: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ProjectionStatus:
    canonical_memories: int
    lexical_entries: int
    vector_entries: int
    canonical_digest: str = ""
    lexical_digest: str = ""
    vector_digest: str = ""

    @property
    def in_sync(self) -> bool:
        """P1-2: digest-based sync — counts can match while content
        differs (e.g. a stale row for a superseded memory). Compare
        stable digests over (memory_id, content) pairs so a rebuild
        that leaves stale content is reported out of sync.
        """
        if not (self.canonical_digest or self.lexical_digest or self.vector_digest):
            # No digest support (legacy/no-op indexes): fall back to counts.
            return self.canonical_memories == self.lexical_entries == self.vector_entries
        return (
            self.canonical_digest == self.lexical_digest == self.vector_digest
            and self.canonical_memories == self.lexical_entries == self.vector_entries
        )


class ProjectionCoordinator:
    def __init__(self, repository: CanonicalRepository, lexical: LexicalIndex, vector: VectorProjection):
        self.repository = repository
        self.lexical = lexical
        self.vector = vector
        self._last_digest: dict[str, str] = {}

    def index_memory(self, memory: Memory) -> None:
        self.lexical.upsert(memory)
        self.vector.upsert(memory)

    def remove_memory(self, memory_id: str) -> None:
        self.lexical.delete(memory_id)
        self.vector.delete(memory_id)

    def rebuild(self, scope: Scope) -> None:
        # Idempotent skip: if the canonical store is unchanged since the
        # last rebuild, the projections are already consistent — full
        # rebuild would be pure waste (the cold-derive super-linear
        # curve's second-order cost: repeated rebuilds per query).
        # Digest comparison is cheap (one list + hash) vs. full
        # clear+reinsert of every memory.
        try:
            current = _content_digest(self.repository.list_memories(scope))
        except Exception:
            current = None  # conservative: rebuild on any digest error
        scope_key = json.dumps(scope.model_dump(), sort_keys=True)
        if current is not None and current == self._last_digest.get(scope_key, None):
            return
        self.lexical.rebuild(self.repository, scope)
        self.vector.rebuild(self.repository, scope)
        if current is not None:
            self._last_digest[scope_key] = current

    def status(self, scope: Scope) -> ProjectionStatus:
        canonical = self.repository.list_memories(scope)
        canonical_digest = _content_digest(canonical)
        lexical_digest = self.lexical.digest(scope)
        vector_digest = self.vector.digest(scope)
        return ProjectionStatus(
            canonical_memories=len(canonical),
            lexical_entries=self.lexical.count(scope),
            vector_entries=self.vector.count(scope),
            canonical_digest=canonical_digest,
            lexical_digest=lexical_digest,
            vector_digest=vector_digest,
        )

    def health(self) -> dict[str, str]:
        """Adapter-neutral readiness probes (P1-2).

        Uses each index's own ``health()`` so SQLite (``connection``)
        and PostgreSQL (``engine``) surfaces both report correctly;
        the service never reaches into index internals.
        """
        components: dict[str, str] = {}
        try:
            components["lexical"] = self.lexical.health()
        except Exception:  # pragma: no cover - defensive
            components["lexical"] = "error"
        try:
            components["vector"] = self.vector.health()
        except Exception:  # pragma: no cover - defensive
            components["vector"] = "error"
        return components


def _content_digest(memories: list[Memory]) -> str:
    """Stable digest over sorted (memory_id, content) pairs.

    Content-equivalent sets (even with different orderings) produce the
    same digest; a stale row with different content produces a
    different digest, catching false-sync that pure counts miss.
    """
    pairs = sorted((memory.id, memory.content) for memory in memories)
    import hashlib

    digest = hashlib.sha256()
    for memory_id, content in pairs:
        digest.update(memory_id.encode())
        digest.update(b"\x00")
        digest.update(content.encode())
        digest.update(b"\x00")
    return digest.hexdigest()
