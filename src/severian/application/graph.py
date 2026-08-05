"""Graph retrieval over the Entity/Relation knowledge graph (Tier 3-1).

The canonical store keeps entities and typed relations
(subject -> predicate -> object) with optional temporal validity. This
service exposes deterministic, scope-bound graph queries:

- ``neighborhood(scope, entity_id, depth)`` — BFS traversal around an
  entity, returning the subgraph (entities + relations) reachable within
  ``depth`` hops. Only CURRENT relations count (a relation with a closed
  ``valid_to`` window is historical).
- ``related_memories(scope, entity_id, depth)`` — memories whose source
  observations mention any entity in the neighborhood, ranked by graph
  distance then deterministically by content. This is the multi-hop /
  graph-assisted retrieval the concept map listed as MISSING.

Design (inspired by the entity-linking + graph-traversal pattern used by
Mem0's graph memory and Zep/Graphiti):
1. resolve the seed entity (by id, or by (kind, name) exact match);
2. BFS over relations in BOTH directions (subject<->object);
3. dedupe deterministically; sort entities by (depth, kind, name) and
   relations by (depth, subject, predicate, object);
4. for related_memories, score each memory by the minimum graph distance
   of any entity it touches, then tiebreak by content (deterministic).

Non-mutating: this service only reads. Scope-bound: every query is
filtered by the exact 5-D scope.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime

from severian.application.ports import CanonicalRepository
from severian.domain.models import Entity, Memory, Relation, Scope

_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)


def _content_tokens(text: str) -> set[str]:
    """Casefolded word-token set for membership matching.

    Used by related_memories to match entity names by TOKEN membership
    rather than substring containment (so 'Al' does not match 'album').
    """
    return {tok.casefold() for tok in _TOKEN_RE.findall(text)}



@dataclass(frozen=True)
class GraphNode:
    entity: Entity
    depth: int

    @property
    def entity_id(self) -> str:
        return self.entity.id


@dataclass
class Neighborhood:
    """Subgraph around a seed entity within ``depth`` hops."""

    seed: Entity
    nodes: list[GraphNode] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    depth: int = 1

    def entity_ids(self) -> list[str]:
        return [node.entity_id for node in self.nodes]

    def summary(self) -> dict[str, object]:
        return {
            "seed": self.seed.name,
            "depth": self.depth,
            "nodes": len(self.nodes),
            "relations": len(self.relations),
        }


@dataclass(frozen=True)
class RelatedMemory:
    memory: Memory
    distance: int
    via_entity_id: str

    @property
    def memory_id(self) -> str:
        return self.memory.id


class GraphRetrievalService:
    """Deterministic graph queries over the entity/relation store."""

    def __init__(self, repository: CanonicalRepository, *, now: datetime | None = None) -> None:
        self._repository = repository
        self._now = now or datetime.now(UTC)

    # -- seed resolution ----------------------------------------------------

    def find_entity(
        self, scope: Scope, *, entity_id: str | None = None,
        name: str | None = None, kind: str | None = None,
    ) -> Entity | None:
        """Resolve a seed entity by id or by (kind, name) exact match."""
        if entity_id is not None:
            try:
                return self._repository.get_entity(scope, entity_id)
            except Exception:
                return None
        if name is not None:
            for entity in self._repository.list_entities(scope):
                if entity.name.casefold() == name.casefold() and (kind is None or entity.kind == kind):
                    return entity
        return None

    # -- traversal ----------------------------------------------------------

    def neighborhood(self, scope: Scope, entity_id: str, *, depth: int = 1) -> Neighborhood:
        """BFS around an entity; returns the subgraph within ``depth`` hops.

        Only CURRENT relations are traversed: a relation whose valid_to
        window has closed is historical and not part of the live graph.
        Deterministic: nodes sorted by (depth, kind, name), relations by
        (depth, subject, predicate, object).
        """
        if depth < 1:
            raise ValueError("depth must be >= 1")
        seed = self._repository.get_entity(scope, entity_id)
        all_relations = self._repository.list_relations(scope)
        current = [r for r in all_relations if self._is_current(r)]

        adjacency: dict[str, list[tuple[str, Relation]]] = {}
        for relation in current:
            adjacency.setdefault(relation.subject_id, []).append((relation.object_id, relation))
            adjacency.setdefault(relation.object_id, []).append((relation.subject_id, relation))

        seen_entities: dict[str, int] = {seed.id: 0}
        seen_relations: dict[str, Relation] = {}
        queue: deque[tuple[str, int]] = deque([(seed.id, 0)])
        while queue:
            current_id, current_depth = queue.popleft()
            if current_depth >= depth:
                continue
            for neighbor_id, relation in adjacency.get(current_id, []):
                seen_relations[relation.id] = relation
                if neighbor_id not in seen_entities:
                    seen_entities[neighbor_id] = current_depth + 1
                    queue.append((neighbor_id, current_depth + 1))

        entities_by_id = {e.id: e for e in self._repository.list_entities(scope)}
        nodes = [
            GraphNode(entity=entities_by_id[eid], depth=d)
            for eid, d in seen_entities.items()
            if eid in entities_by_id
        ]
        nodes.sort(key=lambda node: (node.depth, node.entity.kind, node.entity.name, node.entity.id))
        relations = sorted(
            seen_relations.values(),
            key=lambda r: (r.subject_id, r.predicate, r.object_id, r.id),
        )
        return Neighborhood(seed=seed, nodes=nodes, relations=relations, depth=depth)

    def related_memories(self, scope: Scope, entity_id: str, *, depth: int = 1, limit: int = 10) -> list[RelatedMemory]:
        """Memories touching any entity in the seed's neighborhood.

        A memory 'touches' an entity when one of its source observations
        belongs to the entity's provenance set. Because observations do
        not store entity ids directly, we use the entity's name as a
        deterministic lexical signal against memory content (the same
        token-match approach used by the benchmark temporal fallback).
        Distance = min graph distance; tiebreak by content.
        """
        neighborhood = self.neighborhood(scope, entity_id, depth=depth)
        entities = [node.entity for node in neighborhood.nodes]
        # entity name -> min distance
        name_to_distance = {
            node.entity.name.casefold(): node.depth
            for node in neighborhood.nodes
        }
        hits: list[RelatedMemory] = []
        seen_memory_ids: set[str] = set()
        for memory in self._repository.list_memories(scope):
            content_tokens = _content_tokens(memory.content)
            best: tuple[int, str] | None = None
            for entity in entities:
                entity_name_cf = entity.name.casefold()
                # Match by TOKEN membership, not substring containment:
                # 'Al' must not match 'album' (substring), but must match
                # a memory that contains the standalone token 'Al'.
                if entity_name_cf in content_tokens:
                    candidate = (name_to_distance[entity_name_cf], entity.id)
                    if best is None or candidate[0] < best[0]:
                        best = candidate
            if best is not None and memory.id not in seen_memory_ids:
                seen_memory_ids.add(memory.id)
                hits.append(RelatedMemory(memory=memory, distance=best[0], via_entity_id=best[1]))
        hits.sort(key=lambda hit: (hit.distance, hit.memory.content, hit.memory.id))
        return hits[:limit]

    # -- helpers ------------------------------------------------------------

    def _is_current(self, relation: Relation) -> bool:
        """A relation is current when its validity window is open at now.

        - valid_from in the future  -> not yet valid (exclude)
        - valid_to in the past      -> expired (exclude)
        - open windows (None)       -> current
        """
        if relation.valid_from is not None and relation.valid_from > self._now:
            return False
        if relation.valid_to is not None and relation.valid_to < self._now:
            return False
        return True
