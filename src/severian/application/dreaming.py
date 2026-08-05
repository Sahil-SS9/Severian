"""Dreaming: offline memory consolidation (self-curation engine).

The hippocampal-replay analogue for Severian: a periodic, deterministic,
offline pass that replays a scope's active memories to compress, merge,
and generalise — WITHOUT deleting anything (P0-1 canonical store stays
append-only).

Two services:

- ``ConsolidationService`` ("dreaming"): finds near-duplicate memories,
  supersedes the older head in favour of the newer one (merging source
  provenance), and emits a ``consolidated`` memory that links the group.
  All writes are non-destructive: the superseded memory remains in
  history, the merged sources are preserved on the surviving head.

- ``CurationService`` (self-curation): scores every active memory's
  salience (recency + confidence + usage), flags low-value candidates
  and contradiction candidates, and writes a CurationReport as audit +
  review-marker memories. Removal is NEVER automatic: curation
  proposes, policy/approval disposes.

Both are deterministic (no LLM in the default path — a gateway is
optional, mirroring ``DerivationGateway``), scope-bound, and crash-safe
via the worker's existing job/lease lifecycle.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Scope

_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)

# Duplicate detection: Jaccard overlap of content tokens at or above this
# ratio is a near-duplicate candidate. Deterministic; no embeddings.
_DUPLICATE_JACCARD = 0.8
# Contradiction flag: both directions of overlap above this are a strong
# signal the memories disagree about the same surface.
_CONTRADICTION_JACCARD = 0.6

# Salience half-life (hours): a memory's recency weight halves after this
# much time. 30 days ≈ 720 h.
_SALIENCE_HALFLIFE_H = 720.0
# Confidence weight in the salience blend.
_CONFIDENCE_WEIGHT = 0.4
# Usage weight (per recorded read/boost).
_USAGE_WEIGHT = 0.3
# Recency weight in the salience blend.
_RECENCY_WEIGHT = 0.3


@dataclass(frozen=True)
class DuplicateGroup:
    """A set of memories judged to be near-duplicates of each other."""

    survivor_id: str
    superseded_ids: tuple[str, ...]
    merged_source_ids: tuple[str, ...]
    similarity: float

    @property
    def all_ids(self) -> tuple[str, ...]:
        return (self.survivor_id, *self.superseded_ids)


@dataclass(frozen=True)
class ContradictionCandidate:
    memory_a_id: str
    memory_b_id: str
    overlap: float


@dataclass
class ConsolidationReport:
    """Non-destructive result of one dreaming pass over a scope."""

    scope: Scope
    duplicate_groups: list[DuplicateGroup] = field(default_factory=list)
    contradictions: list[ContradictionCandidate] = field(default_factory=list)
    consolidated_memory_ids: list[str] = field(default_factory=list)
    audit_events: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "scope": self.scope.model_dump(),
            "duplicate_groups": len(self.duplicate_groups),
            "memories_superseded": sum(len(g.superseded_ids) for g in self.duplicate_groups),
            "contradictions": len(self.contradictions),
            "consolidated_memories": len(self.consolidated_memory_ids),
            "audit_events": self.audit_events,
        }


@dataclass
class CurationReport:
    """Self-curation result: salience scoring + proposals, never deletions."""

    scope: Scope
    scored: list[dict[str, Any]] = field(default_factory=list)
    low_value_ids: list[str] = field(default_factory=list)
    contradiction_ids: list[tuple[str, str]] = field(default_factory=list)
    review_marker_ids: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "scope": self.scope.model_dump(),
            "memories_scored": len(self.scored),
            "low_value_candidates": len(self.low_value_ids),
            "contradiction_pairs": len(self.contradiction_ids),
            "review_markers": len(self.review_marker_ids),
        }


def _tokens(content: str) -> set[str]:
    return {tok.casefold() for tok in _TOKEN_RE.findall(content)}


def jaccard(a: str, b: str) -> float:
    """Deterministic token-overlap similarity in [0, 1]."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class ConsolidationService:
    """Dreaming: merge near-duplicates and emit consolidated semantics."""

    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        duplicate_jaccard: float = _DUPLICATE_JACCARD,
        contradiction_jaccard: float = _CONTRADICTION_JACCARD,
        model_id: str = "severian-consolidation-v1",
        prompt_version: str = "deterministic-jaccard-v1",
    ) -> None:
        self._repository = repository
        self._duplicate_jaccard = duplicate_jaccard
        self._contradiction_jaccard = contradiction_jaccard
        self._model_id = model_id
        self._prompt_version = prompt_version

    def consolidate(self, scope: Scope) -> ConsolidationReport:
        """Run one dreaming pass: detect + merge duplicates, flag contradictions.

        Non-destructive:
        - the older duplicate head is superseded (remains in history);
        - the surviving head's source_observation_ids are merged;
        - a ``consolidated`` memory is emitted linking the group;
        - audit events record every action.
        """
        memories = self._repository.list_memories(scope)
        report = ConsolidationReport(scope=scope)

        # 1. Near-duplicate detection (pairwise, deterministic, O(n^2) but
        # bounded: memories per scope are typically small; scale tier later).
        groups = self._find_duplicate_groups(memories)
        for group in groups:
            survivor = self._repository.get_memory(scope, group.survivor_id)
            merged_sources = sorted(set(survivor.source_observation_ids) | set(group.merged_source_ids))
            for old_id in group.superseded_ids:
                self._repository.supersede_memory(scope, old_id, replacement_id=survivor.id)
                self._repository.append_audit(
                    scope,
                    action="dreaming.merge",
                    target_id=old_id,
                    metadata_={"replacement_id": survivor.id, "similarity": group.similarity},
                )
                report.audit_events += 1
            # Emit a consolidated semantic memory linking the group.
            consolidated = self._repository.add_memory(
                Memory(
                    scope=scope,
                    kind="consolidated",
                    content=(
                        f"Consolidated memory group: {survivor.content[:80]!r} "
                        f"unifies {len(group.all_ids)} memories "
                        f"(ids: {', '.join(sorted(group.all_ids)[:5])})"
                    ),
                    source_observation_ids=list(merged_sources or survivor.source_observation_ids),
                    model_id=self._model_id,
                    prompt_version=self._prompt_version,
                    confidence=group.similarity,
                )
            )
            report.consolidated_memory_ids.append(consolidated.id)
            report.duplicate_groups.append(group)

        # 2. Contradiction flags (non-destructive: report only).
        for a, b, overlap in self._find_contradictions(memories):
            report.contradictions.append(ContradictionCandidate(a, b, overlap))

        return report

    def _find_duplicate_groups(self, memories: list[Memory]) -> list[DuplicateGroup]:
        """Group near-duplicates, newest (created_at, then id) survives."""
        groups: list[DuplicateGroup] = []
        used: set[str] = set()
        ordered = sorted(memories, key=lambda m: (m.created_at, m.id))
        for i, mem_a in enumerate(ordered):
            if mem_a.id in used:
                continue
            for mem_b in ordered[i + 1 :]:
                if mem_b.id in used:
                    continue
                sim = jaccard(mem_a.content, mem_b.content)
                if sim >= self._duplicate_jaccard:
                    # The later-created memory survives (newer evidence).
                    survivor, older = mem_b, mem_a
                    groups.append(
                        DuplicateGroup(
                            survivor_id=survivor.id,
                            superseded_ids=(older.id,),
                            merged_source_ids=tuple(older.source_observation_ids),
                            similarity=sim,
                        )
                    )
                    used.update({survivor.id, older.id})
                    break
        return groups

    def _find_contradictions(self, memories: list[Memory]) -> list[tuple[str, str, float]]:
        """Pairs with high overlap that are NOT near-duplicates (same surface,
        different content) — contradiction candidates for review."""
        out: list[tuple[str, str, float]] = []
        for i, mem_a in enumerate(memories):
            for mem_b in memories[i + 1 :]:
                sim = jaccard(mem_a.content, mem_b.content)
                if self._contradiction_jaccard <= sim < self._duplicate_jaccard:
                    out.append((mem_a.id, mem_b.id, sim))
        return out


class CurationService:
    """Self-curation: salience scoring + non-destructive proposals."""

    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        salience_halflife_h: float = _SALIENCE_HALFLIFE_H,
        confidence_weight: float = _CONFIDENCE_WEIGHT,
        usage_weight: float = _USAGE_WEIGHT,
        recency_weight: float = _RECENCY_WEIGHT,
        low_value_threshold: float = 0.15,
        model_id: str = "severian-curation-v1",
        prompt_version: str = "deterministic-salience-v1",
        now: datetime | None = None,
    ) -> None:
        self._repository = repository
        self._halflife = salience_halflife_h
        self._cw = confidence_weight
        self._uw = usage_weight
        self._rw = recency_weight
        self._low_value_threshold = low_value_threshold
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._now = now or datetime.now(UTC)

    def curate(self, scope: Scope) -> CurationReport:
        """Score every active memory and propose (never delete) actions."""
        memories = self._repository.list_memories(scope)
        report = CurationReport(scope=scope)
        for memory in memories:
            age_h = max((self._now - memory.created_at).total_seconds() / 3600.0, 0.0)
            recency = 0.5 ** (age_h / self._halflife)
            usage = getattr(memory, "usage_count", 0.0)
            # usage_count not on Memory; use confidence + recency blend, and
            # any read-tracking would come from projections/outbox (future).
            salience = (
                self._cw * memory.confidence
                + self._rw * recency
                + self._uw * min(usage, 1.0)
            )
            report.scored.append(
                {
                    "memory_id": memory.id,
                    "kind": memory.kind,
                    "salience": round(salience, 4),
                    "recency": round(recency, 4),
                    "confidence": memory.confidence,
                    "age_hours": round(age_h, 1),
                }
            )
            if salience < self._low_value_threshold:
                report.low_value_ids.append(memory.id)
        # Contradiction candidates from the consolidation pass (cheap reuse).
        consolidator = ConsolidationService(self._repository)
        for a, b, _overlap in consolidator._find_contradictions(memories):
            report.contradiction_ids.append((a, b))
        # Write ONE review-marker memory per scope summarising the pass.
        if report.scored:
            marker = self._repository.add_memory(
                Memory(
                    scope=scope,
                    kind="curation_review",
                    content=(
                        f"Curation review: {len(report.scored)} memories scored, "
                        f"{len(report.low_value_ids)} low-value candidates, "
                        f"{len(report.contradiction_ids)} contradiction pairs. "
                        "No memory was deleted; review proposals in audit."
                    ),
                    source_observation_ids=[memories[0].source_observation_ids[0]],
                    model_id=self._model_id,
                    prompt_version=self._prompt_version,
                    confidence=1.0,
                )
            )
            report.review_marker_ids.append(marker.id)
        return report
