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
from severian.infrastructure.vector import _STOPWORDS

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


@dataclass
class RetentionEvaluation:
    """Result of evaluating a scope against a retention policy."""

    scope: Scope
    archived_ids: list[str] = field(default_factory=list)
    expired_ids: list[str] = field(default_factory=list)
    kept_ids: list[str] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "scope": self.scope.model_dump(),
            "archived_candidates": len(self.archived_ids),
            "expired_candidates": len(self.expired_ids),
            "kept": len(self.kept_ids),
            "policy": self.policy,
        }


class RetentionPolicyService:
    """Retention policy evaluation (Tier 2).

    A policy is a dict:
      {"max_age_days": N}             -> memories older than N days are
                                          archive candidates
      {"min_usage": M}                -> memories with usage_count < M AND
                                          older than max_age_days are
                                          archive candidates
      {"kinds": ["episode", "fact"]}  -> restrict evaluation to these kinds

    Non-destructive by default: ``evaluate`` only REPORTS candidates.
    ``apply_archive`` (opt-in) marks candidates superseded with a
    ``retention:archive`` replacement marker — the canonical store keeps
    everything (P0-1); active reads drop archived heads.
    """

    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        now: datetime | None = None,
    ) -> None:
        self._repository = repository
        self._now = now or datetime.now(UTC)

    def evaluate(self, scope: Scope, policy: dict[str, Any]) -> RetentionEvaluation:
        max_age_days = float(policy.get("max_age_days", 0))
        min_usage = int(policy.get("min_usage", 0))
        kinds = set(policy.get("kinds", []))
        eval_result = RetentionEvaluation(scope=scope, policy=policy)
        for memory in self._repository.list_memories(scope):
            if kinds and memory.kind not in kinds:
                eval_result.kept_ids.append(memory.id)
                continue
            age_days = max((self._now - memory.created_at).total_seconds() / 86400.0, 0.0)
            too_old = max_age_days > 0 and age_days >= max_age_days
            low_usage = memory.usage_count < min_usage
            if max_age_days <= 0:
                # No age bound: nothing is an archive candidate on age.
                eval_result.kept_ids.append(memory.id)
                continue
            if too_old and (min_usage <= 0 or low_usage):
                eval_result.archived_ids.append(memory.id)
            else:
                eval_result.kept_ids.append(memory.id)
        return eval_result

    def apply_archive(
        self, scope: Scope, policy: dict[str, Any], *, archive_note: str = "retention:archive"
    ) -> RetentionEvaluation:
        """OPT-IN: supersede archive candidates with a retention marker.

        Non-destructive to the canonical store: the archived memories stay
        in history; active reads and projection rebuilds drop them. The
        marker memory records the policy that archived them.
        """
        eval_result = self.evaluate(scope, policy)
        if eval_result.archived_ids:
            # The marker's source_observation_ids must be REAL observations
            # (add_memory validates sources): gather them from the archived
            # memories' own provenance.
            marker_sources: list[str] = []
            for memory_id in eval_result.archived_ids:
                archived = self._repository.get_memory(scope, memory_id)
                marker_sources.extend(archived.source_observation_ids)
            marker_sources = sorted(set(marker_sources))
            # Invariant: every archived memory has real source observation IDs
            # (the canonical store validates sources on add_memory). The
            # marker must reference only real observations — never fabricate
            # IDs. The previous fallback (archived_ids[:1]) used memory IDs
            # as observation IDs, which could create false provenance.
            assert marker_sources, (
                "retention archive marker requires real source observation IDs "
                "from archived memories, but none were found"
            )
            marker = self._repository.add_memory(
                Memory(
                    scope=scope,
                    kind="retention_archive",
                    content=(
                        f"Retention archive: {len(eval_result.archived_ids)} memories "
                        f"archived by policy {policy} at {self._now.isoformat()}. "
                        "All archived memories remain in history."
                    ),
                    source_observation_ids=marker_sources,
                    model_id="severian-retention-v1",
                    prompt_version="deterministic-policy-v1",
                    confidence=1.0,
                )
            )
            for memory_id in eval_result.archived_ids:
                self._repository.supersede_memory(scope, memory_id, replacement_id=marker.id)
            eval_result.expired_ids = list(eval_result.archived_ids)
            eval_result.archived_ids = []
        return eval_result


class ContradictionScanService:
    """Standalone contradiction scan (Tier 2).

    Extracted from the dreaming pass so it can run independently (e.g. a
    periodic `dream` job with mode=contradictions). Deterministic,
    report-only — never resolves or mutates.
    """

    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        contradiction_jaccard: float = _CONTRADICTION_JACCARD,
        duplicate_jaccard: float = _DUPLICATE_JACCARD,
    ) -> None:
        self._repository = repository
        self._contradiction_jaccard = contradiction_jaccard
        self._duplicate_jaccard = duplicate_jaccard

    def scan(self, scope: Scope) -> list[ContradictionCandidate]:
        memories = self._repository.list_memories(scope)
        return [
            ContradictionCandidate(a, b, overlap)
            for a, b, overlap in self._find(memories)
        ]

    def _find(self, memories: list[Memory]) -> list[tuple[str, str, float]]:
        return detect_contradictions(
            memories,
            contradiction_jaccard=self._contradiction_jaccard,
            duplicate_jaccard=self._duplicate_jaccard,
        )


def _tokens(content: str) -> set[str]:
    return {tok.casefold() for tok in _TOKEN_RE.findall(content)}


# -- MinHash-LSH pre-filter (P1d scale fix) ----------------------------------
# The contradiction scan is O(n^2) on pairwise Jaccard (measured:
# consolidate@10k = 437s). MinHash-LSH buckets memories by token-set
# signature so exact Jaccard is only computed for pairs that share a
# signature band. Deterministic: fixed seed + fixed permutation table.
#
# Parameters (measured 2026-08-05):
# - BANDS=24, ROWS=2: a band is 2 independent min-hash values; a pair is
#   a candidate only when ALL rows of a band match. Collision probability
#   for Jaccard s is 1-(1-s^2)^24 — for s=0.8: ~1.0 (near-dups always
#   candidate); for s=0.1: 1-(0.99)^24 ~= 0.21 (most unrelated pairs
#   pruned). Earlier BANDS=8, ROWS=1 collapsed to all-pairs even on a
#   diverse corpus (measured ratio 0.999) — every pair shared at least
#   one of 8 single-value bands. BANDS=16 ROWS=2 measured 99.9% recall
#   at the 0.5 duplicate band with 0.176 candidate ratio; 24 bands push
#   recall to ~99.98% for the same corpus diversity.
_MINHASH_BANDS = 24
_MINHASH_ROWS = 2
_MINHASH_SEED = 0x5E5E


def _minhash_signature(tokens: set[str]) -> list[int]:
    """Per-band MinHash signature of a token set (deterministic).

    Each band is ROWS independent min-hash values packed into one int
    (each value 32 bits). Two sets with Jaccard s collide on a band with
    probability ~s^ROWS (all rows must match), so shared-band candidates
    are exactly the near-duplicate/contradiction pairs we want.
    """
    signature: list[int] = []
    for band in range(_MINHASH_BANDS):
        salt = f"{_MINHASH_SEED}:{band}"
        packed = 0
        for row in range(_MINHASH_ROWS):
            minimum: int | None = None
            for token in tokens:
                digest = hashlib.sha256((salt + f":{row}" + token).encode("utf-8")).digest()
                value = int.from_bytes(digest[:4], "big")
                if minimum is None or value < minimum:
                    minimum = value
            packed = (packed << 32) | (minimum if minimum is not None else 0)
        signature.append(packed)
    return signature


def _lsh_candidates(memories: list[Memory]) -> list[tuple[int, int]]:
    """Candidate index pairs (i<j) that share >=1 MinHash band.

    Deterministic: bucket by (band, signature-value); any pair landing
    in the same bucket is a candidate. Exact Jaccard is applied later.
    """
    bucket: dict[tuple[int, int], list[int]] = {}
    for index, memory in enumerate(memories):
        # Stopword-filtered tokens: glue words ("the", "was", "at") are
        # present in every doc, so their min-hashes dominate and all
        # docs land in the same buckets. The meaningful vocabulary is
        # what distinguishes documents, so the signature uses only it.
        tokens = {tok for tok in _tokens(memory.content) if tok not in _STOPWORDS}
        if not tokens:
            # Preserve exact-duplicate detection for content whose only
            # tokens are stopwords. The filtered set is empty, so use the
            # full token set as a deterministic fallback signature; exact
            # Jaccard remains the final decision.
            tokens = _tokens(memory.content)
        if not tokens:
            continue
        signature = _minhash_signature(tokens)
        for band, value in enumerate(signature):
            bucket.setdefault((band, value), []).append(index)
    seen: set[tuple[int, int]] = set()
    out: list[tuple[int, int]] = []
    for _band_value, indices in bucket.items():
        if len(indices) < 2:
            continue
        for pos, a in enumerate(indices):
            for b in indices[pos + 1 :]:
                pair = (a, b) if a < b else (b, a)
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
    return out


def jaccard(a: str, b: str) -> float:
    """Deterministic token-overlap similarity in [0, 1]."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def detect_contradictions(
    memories: list[Memory],
    *,
    contradiction_jaccard: float = _CONTRADICTION_JACCARD,
    duplicate_jaccard: float = _DUPLICATE_JACCARD,
) -> list[tuple[str, str, float]]:
    """Find contradiction candidate pairs (shared surface, different content).

    Single source of truth for contradiction detection: returns pairs
    (memory_a_id, memory_b_id, overlap) whose Jaccard overlap is in the
    contradiction band [contradiction_jaccard, duplicate_jaccard).

    Uses MinHash-LSH as a pre-filter (P1d): exact Jaccard is only computed
    for candidate pairs that share a signature band, turning the O(n^2)
    all-pairs scan into O(n*k + candidates). Deterministic: the signature
    uses a fixed seed and fixed salt.

    Used by standalone contradiction scanning, curation, and consolidation
    so there is exactly one LSH->Jaccard implementation.
    """
    out: list[tuple[str, str, float]] = []
    for a_idx, b_idx in _lsh_candidates(memories):
        mem_a = memories[a_idx]
        mem_b = memories[b_idx]
        sim = jaccard(mem_a.content, mem_b.content)
        if contradiction_jaccard <= sim < duplicate_jaccard:
            out.append((mem_a.id, mem_b.id, sim))
    return out


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
        """Group near-duplicates, newest (created_at, then id) survives.

        Uses MinHash-LSH as a candidate generator (same signatures as
        contradiction detection), with exact Jaccard as the final merge
        decision. Deterministic grouping/survivor semantics preserved:
        ordered by (created_at, id), first match wins, later memory survives.
        """
        groups: list[DuplicateGroup] = []
        used: set[str] = set()
        ordered = sorted(memories, key=lambda m: (m.created_at, m.id))
        # Build LSH candidates on the ordered list (same order → same indices).
        candidate_pairs = _lsh_candidates(ordered)
        # Build adjacency: for each ordered index, which other ordered indices
        # are LSH candidates? Sorted for deterministic iteration.
        adjacency: dict[int, list[int]] = {}
        for a_idx, b_idx in candidate_pairs:
            adjacency.setdefault(a_idx, []).append(b_idx)
            adjacency.setdefault(b_idx, []).append(a_idx)
        for i, mem_a in enumerate(ordered):
            if mem_a.id in used:
                continue
            # Check only LSH candidate pairs for this memory, sorted for determinism.
            for j in sorted(adjacency.get(i, [])):
                if j <= i:
                    continue
                mem_b = ordered[j]
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
        different content) — contradiction candidates for review.

        Delegates to the shared detect_contradictions helper (single
        LSH->Jaccard implementation).
        """
        return detect_contradictions(
            memories,
            contradiction_jaccard=self._contradiction_jaccard,
            duplicate_jaccard=self._duplicate_jaccard,
        )


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
            usage = min(memory.usage_count / 100.0, 1.0)  # saturate at 100 hits
            salience = (
                self._cw * memory.confidence
                + self._rw * recency
                + self._uw * usage
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
        # Contradiction candidates via the shared detector (single source of truth).
        for a, b, _overlap in detect_contradictions(memories):
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
