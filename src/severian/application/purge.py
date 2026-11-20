"""What a purge removes, decided once for every backend (M1).

A purge is the only operation that removes text from the canonical store. The *rule*
— which observations, memories, relations and entity names a purge of one observation
touches — is pure computation over the records a backend has already loaded, so it
lives here and both backends (and the in-memory fake) execute exactly the same rule.
Each backend loads its scope's rows, calls :func:`plan_purge` **inside** its write
transaction, and only translates the plan into SQL.

Three pieces:

* :func:`plan_purge` — the plan: the observation revision family, every memory derived
  from it, the relations that cited it, and the entity names nothing else supports.
* :func:`scrub_observation` / :func:`scrub_memory` / :func:`scrub_entity` and
  :func:`drop_purged_support` — the exact payload a purged row becomes.
* :class:`PurgePlan` / :class:`PurgeReport` / :class:`EraseOutcome` — the value objects
  the port returns. Ids and counts only; never content.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from severian.domain.errors import NotFoundError
from severian.domain.models import (
    ERASED,
    PURGE_MARKER,
    Entity,
    Memory,
    Observation,
    Relation,
    revision_family,
)

__all__ = [
    "EraseOutcome",
    "PurgePlan",
    "PurgeReport",
    "drop_purged_support",
    "plan_purge",
    "purge_marker_hash",
    "scrub_entity",
    "scrub_memory",
    "scrub_observation",
]


def purge_marker_hash() -> str:
    """SHA-256 of the fixed removal marker — the only digest a purged row may carry."""
    return hashlib.sha256(PURGE_MARKER.encode()).hexdigest()


@dataclass(frozen=True)
class PurgePlan:
    """Every record id one purge would scrub. Ids and counts only, never content."""

    observation_id: str
    observation_ids: tuple[str, ...]
    memory_ids: tuple[str, ...]
    relation_ids: tuple[str, ...]
    entity_ids: tuple[str, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {
            "observation": len(self.observation_ids),
            "memory": len(self.memory_ids),
            "relation": len(self.relation_ids),
            "entity": len(self.entity_ids),
        }

    def to_dict(self) -> dict[str, Any]:
        """Transport shape for the CLI / dry run: ids by type plus a count per type."""
        return {
            "observation_id": self.observation_id,
            "observation_ids": list(self.observation_ids),
            "memory_ids": list(self.memory_ids),
            "relation_ids": list(self.relation_ids),
            "entity_ids": list(self.entity_ids),
            "counts": self.counts,
        }


@dataclass(frozen=True)
class PurgeReport:
    """What a purge actually changed. A repeated purge reports the same plan and zero writes."""

    plan: PurgePlan
    observations: int = 0
    memories: int = 0
    relations: int = 0
    relations_retracted: int = 0
    entities: int = 0
    audit_rows: int = 0
    cancelled_jobs: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "observations": self.observations,
            "memories": self.memories,
            "relations": self.relations,
            "relations_retracted": self.relations_retracted,
            "entities": self.entities,
            "audit_rows": self.audit_rows,
            "cancelled_jobs": self.cancelled_jobs,
        }


@dataclass(frozen=True)
class EraseOutcome:
    """What the atomic erasure cascade changed, in one transaction (MG-3)."""

    observation_id: str
    tombstone_id: str
    memory_ids: tuple[str, ...] = field(default_factory=tuple)
    relations_retracted: int = 0


def _cites_the_chain(relation: Relation, chain_ids: set[str]) -> bool:
    """True when ``relation`` cited any observation in the chain, now or in its history.

    ``source_observation_ids`` is the current support; ``retracted_for_ids`` is what an
    erasure or revision already withdrew from it. Both are attributable provenance for
    THIS relation, so both make it a purge target — the reach stays per-relation, never
    "every retracted relation in the graph" (M1 F1).
    """
    return bool(
        chain_ids.intersection(relation.source_observation_ids)
        or chain_ids.intersection(relation.retracted_for_ids)
    )


def _still_in_the_graph(relation: Relation, chain_ids: set[str]) -> bool:
    """True when ``relation`` keeps a support after the chain is purged (or supports unknown).

    A retracted relation supports nothing. A relation with no lineage at all (added by
    hand, so its provenance is unknown) counts as support: one source's purge must never
    retract or scrub something whose support it cannot see. Conservative by design —
    retain, do not scrub.
    """
    if relation.retracted_at is not None:
        return False
    if not relation.source_observation_ids:
        return True
    return any(source not in chain_ids for source in relation.source_observation_ids)


def plan_purge(
    *,
    observation_id: str,
    observations: Sequence[Observation],
    memories: Sequence[Memory],
    relations: Sequence[Relation],
    entities: Sequence[Entity],
) -> PurgePlan:
    """Every record a purge of ``observation_id`` must scrub, decided from loaded rows.

    The rules, in order:

    1. **The whole observation revision family** (root → revisions → erasure tombstone).
       Purging one revision of a chain purges the chain: an older revision holds the same
       text, so leaving it would defeat the purge.
    2. **Every memory that cites any observation in the chain**, plus that memory's own
       revision family (superseded revisions and tombstones hold derived text too).
    3. **Every relation that cited any observation in the chain.** A relation that keeps
       another support stays in the graph with its lineage shortened; one that loses its
       last support is retracted (kept as history, no text). A relation the chain once
       supported and an erasure or revision already withdrew it from is a target too:
       that withdrawn citation is still attributable provenance for it, and without it the
       erase-first order would leave the relation and the entity names it alone carried
       untouched (M1 F1).
    4. **The entity names nothing else supports** — entities referenced by those relations,
       minus any entity another live relation still references. Shared entities keep their
       name: only unsupported ones are scrubbed.

    Raises ``NotFoundError`` when the observation is not among the loaded rows (callers load
    one scope, so a foreign-scope id lands here too and leaks nothing).
    """
    by_observation = {row.id: row for row in observations}
    if observation_id not in by_observation:
        raise NotFoundError("observation not found")
    chain = revision_family(by_observation, observation_id)
    chain_ids = {row.id for row in chain}

    memories_by_id = {row.id: row for row in memories}
    derived: set[str] = {row.id for row in memories if chain_ids.intersection(row.source_observation_ids)}
    # A memory's own lineage can hold revisions whose source list was inherited, so take
    # the whole family of anything that matched: no sibling revision keeps the text.
    for memory_id in list(derived):
        if memory_id in memories_by_id:
            derived.update(row.id for row in revision_family(memories_by_id, memory_id))

    cited = [row for row in relations if _cites_the_chain(row, chain_ids)]
    cited_ids = {row.id for row in cited}

    # An entity is "supported" when some relation is still in the graph and keeps a
    # support that is not being purged. A cited relation that loses its last support
    # supports nothing, so it never shields its endpoints from scrubbing.
    supported: set[str] = set()
    for row in relations:
        if _still_in_the_graph(row, chain_ids):
            supported.update((row.subject_id, row.object_id))
    candidates = {endpoint for row in cited for endpoint in (row.subject_id, row.object_id)}
    entity_ids = {row.id for row in entities if row.id in candidates and row.id not in supported}

    return PurgePlan(
        observation_id=observation_id,
        observation_ids=tuple(row.id for row in chain),
        memory_ids=tuple(sorted(derived)),
        relation_ids=tuple(sorted(cited_ids)),
        entity_ids=tuple(sorted(entity_ids)),
    )


def _never_active(current: str | None) -> str:
    """The link a purged record keeps: its own successor, or the erasure sentinel."""
    return current if current is not None else ERASED


def scrub_observation(observation: Observation) -> Observation:
    """The purged form of an observation: marker text, marker digests, no metadata."""
    digest = purge_marker_hash()
    return observation.model_copy(
        update={
            "content": PURGE_MARKER,
            "content_hash": digest,
            "raw_content": PURGE_MARKER,
            "raw_hash": digest,
            "source": {},
            "attachments": [],
            "purged": True,
            "superseded_by_id": _never_active(observation.superseded_by_id),
        }
    )


def scrub_memory(memory: Memory) -> Memory:
    """The purged form of a memory: marker text, no cues or trigger, never active."""
    return memory.model_copy(
        update={
            "content": PURGE_MARKER,
            "cues": [],
            "trigger": None,
            "purged": True,
            "superseded_by_id": _never_active(memory.superseded_by_id),
        }
    )


def scrub_entity(entity: Entity) -> Entity:
    """The purged form of an entity: the marker as its name."""
    return entity.model_copy(update={"name": PURGE_MARKER, "purged": True})


def drop_purged_support(relation: Relation, *, purged_ids: Iterable[str], at: datetime) -> Relation:
    """``relation`` with the purged supports dropped; retracted when none is left.

    Both provenance lists lose the purged ids: the current support stops counting (as
    before) and the withdrawn-history record stops naming a record that no longer carries
    text — so a repeat purge finds nothing left here and audits nothing. ``at`` is the
    retraction timestamp, supplied by the backend so both use one clock.
    """
    dropped = set(purged_ids)
    lineage = [source for source in relation.source_observation_ids if source not in dropped]
    history = [source for source in relation.retracted_for_ids if source not in dropped]
    retracted_at = relation.retracted_at
    if not lineage and relation.source_observation_ids:
        retracted_at = retracted_at or at
    return relation.model_copy(
        update={
            "source_observation_ids": lineage,
            "retracted_for_ids": history,
            "retracted_at": retracted_at,
        }
    )
