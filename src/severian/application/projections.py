from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from severian.application.ports import CanonicalRepository, LexicalIndex
from severian.domain.errors import NotFoundError
from severian.domain.hits import VectorHit
from severian.domain.models import Event, Memory, Scope

# Outbox event kinds that touch the memory projections (see ProjectionCoordinator.apply).
_PROJECTED_EVENTS = frozenset({"memory.created", "memory.revised", "memory.rolled_back"})


@runtime_checkable
class VectorProjection(Protocol):
    """Duck-typed vector projection surface (LocalVectorIndex or PGVectorIndex)."""

    def upsert(self, memory: Memory) -> None: ...
    def upsert_many(self, memories: list[Memory]) -> None:
        """Bulk upsert in ONE transaction (zero-debt 3.3)."""
        for memory in memories:
            self.upsert(memory)
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

    def apply(self, events: list[Event]) -> None:
        """Project outbox events incrementally (zero-debt 3.2/3.3).

        The outbox is the projection delta contract. Each event names
        the memory it affects; applying it touches ONLY that memory's
        rows — never a scope-wide clear+reinsert — so a one-memory
        update costs bounded writes independent of scope size.

        Idempotent by construction: upsert is an ON CONFLICT UPDATE and
        delete of a missing id is a no-op, so repeated application,
        duplicate delivery, out-of-order delivery, and crash-retry all
        converge to the same projection state. Scope isolation holds
        because every event carries its own scope and we address rows
        by memory id.

        Event semantics (which memory ids an event touches):
        - ``memory.created``     → the named memory
        - ``memory.revised``     → the revision and its old head
          (``revision_of_id``)
        - ``memory.rolled_back`` → the restored memory and the demoted
          head (the memory whose ``superseded_by_id`` points at it)
        - anything else          → nothing (no projection impact)

        Every touched id then converges to its CURRENT canonical state:
        an active head is upserted, anything else (superseded, erased,
        tombstone, missing) is deleted. Deciding by canonical state rather
        than by event kind keeps the projection to active heads only, so
        a late ``memory.created`` for a memory corrected since, or an
        erasure tombstone's own creation event, can never re-index a
        stale fact, and the result does not depend on event order.

        zero-debt 3.3: all upserts in the batch are collected and
        committed via ``upsert_many`` — ONE transaction, never per
        event.
        """
        touched: dict[str, Scope] = {}
        for event in events:
            if event.kind not in _PROJECTED_EVENTS:
                continue
            memory = self.repository.get_memory(event.scope, str(event.payload["id"]))
            touched.setdefault(memory.id, event.scope)
            if event.kind == "memory.revised" and memory.revision_of_id is not None:
                touched.setdefault(memory.revision_of_id, event.scope)
            elif event.kind == "memory.rolled_back":
                # The demoted head is now SUPERSEDED, so it is invisible
                # to list_memories (active-only) — scan the full history.
                for candidate in self.repository.list_memories_history(event.scope):
                    if candidate.superseded_by_id == memory.id:
                        touched.setdefault(candidate.id, event.scope)
        upserts: list[Memory] = []
        deletes: list[str] = []
        for memory_id, scope in touched.items():
            try:
                current = self.repository.get_memory(scope, memory_id)
            except NotFoundError:
                deletes.append(memory_id)
                continue
            if current.superseded_by_id is None:
                upserts.append(current)
            else:
                deletes.append(memory_id)
        if upserts:
            self.lexical.upsert_many(upserts)
            self.vector.upsert_many(upserts)
        for memory_id in deletes:
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
