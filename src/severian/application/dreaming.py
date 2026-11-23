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
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from severian.application.dreamtriage import (
    COMPATIBLE,
    CONTRADICT,
    MAX_EDITS_PER_PAIR,
    MAX_FRAME_VALUES,
    NEGATIONS,
    PairVerdict,
    Statement,
    Triage,
    ValueEdit,
    classify_pair,
    proper_tokens,
    statements,
    value_edits,
)
from severian.application.dreamwriter import DreamWriter
from severian.application.message_units import MESSAGE_UNIT_KIND
from severian.application.ports import CanonicalRepository
from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import Memory, Scope
from severian.domain.text import STOPWORDS

_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)

# Duplicate detection: Jaccard overlap of content tokens at or above this
# ratio is a near-duplicate candidate. Deterministic; no embeddings.
_DUPLICATE_JACCARD = 0.8
# Contradiction flag: both directions of overlap above this are a strong
# signal the memories disagree about the same surface.
_CONTRADICTION_JACCARD = 0.6

# Rounds one consolidation pass may take to reach a fixed point. Each round strictly reduces the
# number of active memories (every group supersedes at least one), so this is a stop, not a search.
MAX_CONSOLIDATION_ROUNDS = 5

# Salience half-life (hours): a memory's recency weight halves after this
# much time. 30 days ≈ 720 h.
_SALIENCE_HALFLIFE_H = 720.0
# Confidence weight in the salience blend.
_CONFIDENCE_WEIGHT = 0.4
# Recency weight in the salience blend.
#
# There is no usage term (S2, decision 4). Usage tracking is never on: nothing in ``src/`` calls
# ``search(track_usage=True)``, so every ``usage_count`` in every store is 0 and the old
# ``0.3 * min(usage_count / 100, 1)`` term always contributed exactly 0.0. Removing it is
# byte-identical in practice and stops a dormant-but-plausible knob from silently changing what
# curation proposes. ``tests/application/test_dream_usage_salience.py`` pins both halves of that.
_RECENCY_WEIGHT = 0.3



def _dream_input(repository: CanonicalRepository, scope: Scope, exclude_kinds: Collection[str]) -> list[Memory]:
    """Active memories a dream service may read.

    Drops the caller's ``exclude_kinds`` and, always, S1 ``message_unit`` memories: a unit is a
    search-only copy of one message from a turn the store already holds, not a memory of the user.
    Reading them would make the pass merge, score or archive copies and multiplies its input.
    """
    return [
        memory
        for memory in repository.list_memories(scope)
        if memory.kind not in exclude_kinds and memory.kind != MESSAGE_UNIT_KIND
    ]


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
    #: Dry run (``consolidate(..., dry_run=True)``): what the pass would do, nothing written.
    dry_run: bool = False
    #: Pairs the deterministic triage classified, by class (``dreamtriage.PAIR_KINDS``).
    triage: dict[str, int] = field(default_factory=dict)
    #: Supersedes the guarded path refused (a concurrent close/erase/supersede won): skipped, not failed.
    skipped_supersedes: int = 0
    #: The model writer's cost record, when one was configured. Empty with no writer (the default).
    writer: dict[str, Any] = field(default_factory=dict)
    #: What a dry run would write, in the order it would write it.
    planned: list[dict[str, Any]] = field(default_factory=list)
    #: How many detect-and-merge rounds it took to reach a fixed point (1 unless duplicates chained).
    rounds: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "scope": self.scope.model_dump(),
            "dry_run": self.dry_run,
            "duplicate_groups": len(self.duplicate_groups),
            "memories_superseded": sum(len(g.superseded_ids) for g in self.duplicate_groups),
            "contradictions": len(self.contradictions),
            "consolidated_memories": len(self.consolidated_memory_ids),
            "skipped_supersedes": self.skipped_supersedes,
            "audit_events": self.audit_events,
            "triage": self.triage,
            "writer": self.writer,
            "rounds": self.rounds,
        }


@dataclass
class CurationReport:
    """Self-curation result: salience scoring + proposals, never deletions."""

    scope: Scope
    scored: list[dict[str, Any]] = field(default_factory=list)
    low_value_ids: list[str] = field(default_factory=list)
    contradiction_ids: list[tuple[str, str]] = field(default_factory=list)
    review_marker_ids: list[str] = field(default_factory=list)
    #: Dry run: the scoring and the proposals, with no marker and no report row written.
    dry_run: bool = False
    #: Pairs the deterministic triage classified, by class (``dreamtriage.PAIR_KINDS``).
    triage: dict[str, int] = field(default_factory=dict)
    # Stable identity of THIS pass, so the audit trail can name the report it
    # summarises (the review marker keeps the prose summary).
    report_id: str = field(default_factory=lambda: str(uuid4()))

    def summary(self) -> dict[str, Any]:
        return {
            "scope": self.scope.model_dump(),
            "report_id": self.report_id,
            "dry_run": self.dry_run,
            "memories_scored": len(self.scored),
            "low_value_candidates": len(self.low_value_ids),
            "contradiction_pairs": len(self.contradiction_ids),
            "review_markers": len(self.review_marker_ids),
            "triage": self.triage,
        }

    def reference(self) -> dict[str, Any]:
        """What the audit row carries: the ids and counts, never memory content.

        The review marker this report belongs to is the audit row's ``target_id``
        and is deliberately NOT repeated here: the write decides which stored
        memory the row names (an identical marker already in the scope is
        reused), so a second copy of the id could disagree with the row it rides
        on.
        """
        return {
            "report_id": self.report_id,
            "memories_scored": len(self.scored),
            "low_value_candidates": len(self.low_value_ids),
            "low_value_ids": list(self.low_value_ids),
            "contradiction_pairs": len(self.contradiction_ids),
            "contradiction_ids": [list(pair) for pair in self.contradiction_ids],
        }


@dataclass
class RetentionEvaluation:
    """Result of evaluating a scope against a retention policy."""

    scope: Scope
    archived_ids: list[str] = field(default_factory=list)
    expired_ids: list[str] = field(default_factory=list)
    kept_ids: list[str] = field(default_factory=list)
    policy: dict[str, Any] = field(default_factory=dict)
    #: Dry run: the plan only, nothing archived.
    dry_run: bool = False
    #: Supersedes the guarded path refused (lost race): skipped and logged, never a failure.
    skipped: int = 0

    def summary(self) -> dict[str, Any]:
        return {
            "scope": self.scope.model_dump(),
            "dry_run": self.dry_run,
            "archived_candidates": len(self.archived_ids),
            "expired_candidates": len(self.expired_ids),
            "kept": len(self.kept_ids),
            "skipped": self.skipped,
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

    def evaluate(
        self,
        scope: Scope,
        policy: dict[str, Any],
        *,
        exclude_kinds: Collection[str] = (),
        dry_run: bool = False,
    ) -> RetentionEvaluation:
        max_age_days = float(policy.get("max_age_days", 0))
        min_usage = int(policy.get("min_usage", 0))
        kinds = set(policy.get("kinds", []))
        eval_result = RetentionEvaluation(scope=scope, policy=policy, dry_run=dry_run)
        for memory in _dream_input(self._repository, scope, exclude_kinds):
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
        self,
        scope: Scope,
        policy: dict[str, Any],
        *,
        archive_note: str = "retention:archive",
        exclude_kinds: Collection[str] = (),
        dry_run: bool = False,
    ) -> RetentionEvaluation:
        """OPT-IN: supersede archive candidates with a retention marker.

        Non-destructive to the canonical store: the archived memories stay
        in history; active reads and projection rebuilds drop them. The
        marker memory records the policy that archived them.

        The plan is written down FIRST (``retention.archive_plan``, an id-and-counts row) and only
        then carried out, so a shared store always has the dry run recorded beside the apply
        (owner decision 5). ``dry_run`` stops after the plan: nothing is written at all.
        """
        eval_result = self.evaluate(scope, policy, exclude_kinds=exclude_kinds, dry_run=dry_run)
        if eval_result.archived_ids and dry_run:
            return eval_result
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
            marker, _row = self._repository.add_memory_with_audit(
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
                ),
                action="retention.archive",
                metadata_={"policy": dict(policy), "candidates": len(eval_result.archived_ids)},
            )
            # The plan, written BEFORE anything is archived: the dry run a shared store must have.
            self._repository.append_audit(
                scope,
                action="retention.archive_plan",
                target_id=marker.id,
                metadata_={
                    "policy": dict(policy),
                    "memory_ids": sorted(eval_result.archived_ids),
                    "note": archive_note,
                },
            )
            for memory_id in eval_result.archived_ids:
                # OPS-05: idempotent re-run — a second apply_archive on the
                # same scope must not crash on already-archived memories.
                archived = self._repository.get_memory(scope, memory_id)
                if archived.superseded_by_id is not None:
                    continue
                try:
                    self._repository.supersede_memory(scope, memory_id, replacement_id=marker.id)
                except ConflictError as exc:
                    # Owner decision 7: a lost race is skipped and logged, never a failed pass.
                    eval_result.skipped += 1
                    self._repository.append_audit(
                        scope,
                        action="retention.skip",
                        target_id=memory_id,
                        metadata_={"reason": str(exc), "replacement_id": marker.id},
                    )
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

    def scan(self, scope: Scope, *, exclude_kinds: Collection[str] = ()) -> list[ContradictionCandidate]:
        memories = _dream_input(self._repository, scope, exclude_kinds)
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


# -- zero-debt 5.2: dense-degeneration repair ---------------------------------
# Task 5.1 evidence: on shared-scaffold corpora the unigram MinHash
# signature is dominated by scaffold tokens present in every document,
# so all documents land in the same buckets -> candidate_ratio 1.000
# (measured: template corpus max bucket = the ENTIRE corpus, 10k).
#
# Repair (lossless, deterministic):
#   A. Exact-duplicate condensation: identical contents collapse to ONE
#      representative; pairs within a digest group ARE duplicates (the
#      duplicate gate itself), so they are emitted directly and the
#      representative only is LSH-bucketed. 10000 template docs with 64
#      unique combos -> 64 representatives, candidate_ratio ~4e-5. No
#      pair lost: identical content is the strongest possible duplicate
#      evidence.
#
# Evidence check (Task 5.2, measured): the ORIGINAL stopword-filtered
# signature already scores ratio 0.341 / 0 missing on a genuinely
# diverse corpus; the dense-corpus explosion is exact-duplicate
# condensation loss, not band structure. DF-pruning was tried and
# REJECTED: shrinking the signature token set lowered Jaccard between
# near-duplicates enough to regress recall (2 pairs missed on the
# diverse corpus, vs 0 missing with the original signature) and exposed
# a pathological fixed-salt interaction (band collision 0/24 on a
# J=0.556 pair where theory predicts ~0.31/band). Condensation alone
# fixes the 1.000 ratio without touching recall.
#
# Plus: token caching (one _tokens() call per document, reused for
# signature AND Jaccard) and bounded batched candidate iteration with
# checkpoint/resume for long offline consolidation jobs.
_STOPWORD_HEAVY_FALLBACK_TOKENS = 2


def _candidate_batches(
    memories: list[Memory],
    *,
    batch_size: int = 50_000,
) -> Iterable[list[tuple[int, int]]]:
    """Bounded-memory candidate generator (zero-debt 5.2).

    Yields candidate index pairs in batches of at most ``batch_size``.
    Callers that checkpoint between batches (consolidation) can resume a
    long offline job without holding the full candidate set in memory.

    Semantics are identical to :func:`_lsh_candidates` — the same two
    passes (digest condensation, MinHash LSH) — but pairs are produced
    incrementally instead of materialised. Deterministic ordering: digest
    groups first (by rep index), then LSH bucket pairs in bucket-sorted
    order.

    Evidence (Task 5.1/5.2): the dense-corpus candidate explosion is
    driven by exact-duplicate condensation loss, NOT by the LSH band
    structure. The template corpus (shared scaffold) collapsed to
    candidate_ratio 1.000 because 10k docs contain only ~64 unique
    contents; identical contents share every band. Condensing identical
    contents to one representative (lossless: identical contents are
    exact duplicates by definition) drops the template ratio to ~4e-5
    while the stopword-filtered signature preserves the measured 0.34
    ratio / 100% recall on genuinely diverse corpora.
    """
    contents = [m.content for m in memories]

    # Pass A: exact-duplicate condensation.
    digest_groups: dict[str, list[int]] = {}
    for idx, content in enumerate(contents):
        digest_groups.setdefault(_content_digest(content), []).append(idx)
    reps: list[int] = []
    for _digest, indices in sorted(digest_groups.items()):
        rep = indices[0]
        reps.append(rep)
        group_pairs = [(a, b) for pos, a in enumerate(indices) for b in indices[pos + 1 :]]
        for start in range(0, len(group_pairs), batch_size):
            yield group_pairs[start : start + batch_size]

    # Pass B: LSH over representatives only, stopword-filtered signature
    # (the pre-5.2 signature, whose recall is proven: 0 missing pairs on
    # the diverse corpus). Token sets are cached so detect_contradictions
    # reuses them for the exact-Jaccard pass.
    rep_tokens: dict[int, set[str]] = {}
    for rep in reps:
        tokens = {tok for tok in _tokens(memories[rep].content) if tok not in STOPWORDS}
        if not tokens:
            # Stopword-only content: fall back to the full token set so
            # exact duplicates still share a deterministic signature
            # (exact Jaccard remains the final decision).
            tokens = _tokens(memories[rep].content)
        rep_tokens[rep] = tokens

    bucket: dict[tuple[int, int], list[int]] = {}
    for rep in reps:
        toks = rep_tokens[rep]
        if not toks:
            continue  # all-stopword content: jaccard vs anything is 0
        signature = _minhash_signature(toks)
        for band, value in enumerate(signature):
            bucket.setdefault((band, value), []).append(rep)
        if len(toks) <= _STOPWORD_HEAVY_FALLBACK_TOKENS:
            # Stopword-heavy doc (<=2 meaningful tokens): the filtered
            # signature is dominated by the unique tokens, so pairs that
            # share ONLY stopwords (jaccard gate says duplicate/contradiction
            # at 0.96) would never collide. Add the full-token signature as
            # a second blocking key. Bounded: such docs are rare in real
            # corpora (template/diverse have 6-13 filtered tokens), so the
            # fallback cannot re-create dense degeneration.
            full_sig = _minhash_signature(_tokens(memories[rep].content))
            for band, value in enumerate(full_sig):
                bucket.setdefault((band, value), []).append(rep)

    seen: set[tuple[int, int]] = set()
    for _band_value, indices in bucket.items():
        if len(indices) < 2:
            continue
        pairs = []
        for pos, a in enumerate(indices):
            for b in indices[pos + 1 :]:
                pair = (a, b) if a < b else (b, a)
                if pair in seen:
                    continue
                seen.add(pair)
                pairs.append(pair)
                if len(pairs) >= batch_size:
                    yield pairs
                    pairs = []
        if pairs:
            yield pairs


def _lsh_candidates(memories: list[Memory]) -> list[tuple[int, int]]:
    """Candidate index pairs (i<j) that share >=1 MinHash band.

    Deterministic: bucket by (band, signature-value); any pair landing
    in the same bucket is a candidate. Exact Jaccard is applied later.

    zero-debt 5.2: exact-duplicate condensation + DF-pruned signatures
    (see ``_candidate_batches``). Output is the materialised form of the
    same passes — identical pair set, deterministic order.
    """
    out: list[tuple[int, int]] = []
    for batch in _candidate_batches(memories):
        out.extend(batch)
    return out


def jaccard(a: str, b: str) -> float:
    """Deterministic token-overlap similarity in [0, 1]."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _tokens_cached(contents: list[str]) -> list[set[str]]:
    """Tokenize each content exactly once (zero-debt 5.2 token cache).

    The LSH signature pass and the exact-Jaccard pass used to tokenize
    every pair independently — O(2 * n^2) tokenizations on a dense
    corpus. With the cache, each document is tokenized once and both
    passes reuse the sets.
    """
    return [_tokens(c) for c in contents]


def _jaccard_tokens(ta: set[str], tb: set[str]) -> float:
    """Jaccard over already-tokenized sets (mirror of :func:`jaccard`)."""
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

    zero-debt 5.2: content tokens are cached once per document and reused
    by both the signature pass (via ``_candidate_batches``) and the exact
    Jaccard pass here, eliminating the per-pair re-tokenization cost.

    Used by standalone contradiction scanning, curation, and consolidation
    so there is exactly one LSH->Jaccard implementation.
    """
    classified = triage(
        memories,
        duplicate_jaccard=duplicate_jaccard,
        contradiction_jaccard=contradiction_jaccard,
    )
    return [(verdict.memory_a_id, verdict.memory_b_id, verdict.overlap) for verdict in classified.contradictions]


def triage(
    memories: list[Memory],
    *,
    duplicate_jaccard: float = _DUPLICATE_JACCARD,
    contradiction_jaccard: float = _CONTRADICTION_JACCARD,
) -> Triage:
    """Classify every LSH-candidate pair as duplicate, restate or contradict (code, never a model).

    One candidate scan feeds all three classes, so a pass can never see a pair as a duplicate in
    one place and a contradiction in another. The rules live in
    :func:`severian.application.dreamtriage.classify_pair`; this function only enumerates
    candidates and measures them.
    """
    tokens = _tokens_cached([m.content for m in memories])
    proper_by_index = [proper_tokens(m.content) for m in memories]
    verdicts = Triage()
    # 0.6.2: statements are read lazily, only for memories in a value-bearing pair.
    statements_by_index: dict[int, tuple[Statement, ...]] = {}
    edits_by_verdict: dict[int, list[ValueEdit]] = {}

    def statements_of(index: int) -> tuple[Statement, ...]:
        if index not in statements_by_index:
            statements_by_index[index] = statements(memories[index].content)
        return statements_by_index[index]

    for a_idx, b_idx in _lsh_candidates(memories):
        tokens_a = tokens[a_idx]
        tokens_b = tokens[b_idx]
        sim = _jaccard_tokens(tokens_a, tokens_b)
        if sim < contradiction_jaccard:
            continue
        # A negation is never noise: "has a garden" against "has not a garden" differs by one
        # stopword, and that stopword is the whole fact.
        differing = {
            tok for tok in tokens_a ^ tokens_b if tok not in STOPWORDS or tok in NEGATIONS
        }
        kind = classify_pair(
            overlap=sim,
            differing=differing,
            proper=proper_by_index[a_idx] | proper_by_index[b_idx],
            duplicate_jaccard=duplicate_jaccard,
            contradiction_jaccard=contradiction_jaccard,
        )
        if kind is None:
            continue
        if kind == CONTRADICT:
            # A value-bearing difference is a contradiction only when one value changes inside ONE
            # shared statement (dreamtriage.value_edits). Anything else can be true together. The
            # demotion is to COMPATIBLE, never to DUPLICATE: duplicate decisions do not move.
            edits = value_edits(statements_of(a_idx), statements_of(b_idx))
            if not edits or len(edits) > MAX_EDITS_PER_PAIR:
                kind = COMPATIBLE
            else:
                edits_by_verdict[len(verdicts.verdicts)] = edits
        verdicts.verdicts.append(
            PairVerdict(
                memory_a_id=memories[a_idx].id,
                memory_b_id=memories[b_idx].id,
                kind=kind,
                overlap=sim,
                differing=tuple(sorted(differing)),
            )
        )
    _demote_running_figures(verdicts, edits_by_verdict)
    return verdicts


def _demote_running_figures(verdicts: Triage, edits_by_verdict: dict[int, list[ValueEdit]]) -> None:
    """A frame seen with more than MAX_FRAME_VALUES distinct values in this pass is a log, not a fact.

    "the disk usage reached _ percent" with twenty readings is a series; flagging every pair of
    readings is how one log line became hundreds of "contradictions". A pair stays a contradiction
    while at least one of its edits sits in a frame with few values (a fact that changed a few times).
    """
    values_by_frame: dict[tuple[tuple[str, ...], tuple[str, ...]], set[tuple[str, ...]]] = {}
    for edits in edits_by_verdict.values():
        for edit in edits:
            values_by_frame.setdefault(edit.frame, set()).update((edit.value_a, edit.value_b))
    for index, edits in edits_by_verdict.items():
        if all(len(values_by_frame[edit.frame]) > MAX_FRAME_VALUES for edit in edits):
            verdicts.verdicts[index] = replace(verdicts.verdicts[index], kind=COMPATIBLE)


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
        writer: DreamWriter | None = None,
    ) -> None:
        self._repository = repository
        self._duplicate_jaccard = duplicate_jaccard
        self._contradiction_jaccard = contradiction_jaccard
        self._model_id = model_id
        self._prompt_version = prompt_version
        #: Optional text writer for the merge summary. It writes words, never decisions.
        self._writer = writer

    def consolidate(
        self,
        scope: Scope,
        *,
        exclude_kinds: Collection[str] = (),
        dry_run: bool = False,
    ) -> ConsolidationReport:
        """Run one dreaming pass: detect + merge duplicates, flag contradictions.

        Non-destructive:
        - the older duplicate head is superseded (remains in history);
        - the surviving head's source_observation_ids are merged;
        - a ``consolidated`` memory is emitted linking the group;
        - audit events record every action.

        ``exclude_kinds`` keeps kinds out of the INPUT (the dreaming pass excludes its own markers
        so a pass cannot read its own last record as a fact; see ``markers.MARKER_KINDS``).
        ``dry_run`` computes the identical plan and writes nothing — the operator command's default.
        """
        memories = _dream_input(self._repository, scope, exclude_kinds)
        report = ConsolidationReport(scope=scope, dry_run=dry_run)
        totals: dict[str, int] = {}
        # One pass reaches a fixed point: the greedy pairwise grouping can leave a longer duplicate
        # chain one merge short ("a"~"b" merges, "b"~"c" is left), which is how a second pass used to
        # find another duplicate on a real store. Each round strictly reduces the number of active
        # memories, so the loop terminates; MAX rounds is a stop, and it is reported when it is hit.
        classified = triage(
            memories,
            duplicate_jaccard=self._duplicate_jaccard,
            contradiction_jaccard=self._contradiction_jaccard,
        )
        planned_superseded: set[str] = set()
        blocked: set[str] = set()
        remaining = list(memories)
        for round_number in range(1, MAX_CONSOLIDATION_ROUNDS + 1):
            # 1. Deterministic triage FIRST (owner decision 3): code says what each candidate pair is.
            #    A model may only write the summary text later, and only if one is configured.
            classified = triage(
                remaining,
                duplicate_jaccard=self._duplicate_jaccard,
                contradiction_jaccard=self._contradiction_jaccard,
            )
            for kind, count in classified.counts().items():
                totals[kind] = totals.get(kind, 0) + count
            groups = self._find_duplicate_groups(remaining, classified)
            if not groups:
                break
            self._check_duplicate_groups(scope, groups)
            # The merge summaries are the ONLY thing a model may write. Code has already decided the
            # groups; a failed batch costs nothing but a line in the writer's counters.
            blocked |= self._write_groups(
                scope,
                groups,
                self._summaries(remaining, groups, report, dry_run=dry_run),
                report,
                dry_run=dry_run,
                planned_superseded=planned_superseded,
            )
            report.rounds = round_number
            if round_number == MAX_CONSOLIDATION_ROUNDS:
                break
            before = {memory.id for memory in remaining}
            remaining = [
                memory
                for memory in _dream_input(self._repository, scope, exclude_kinds)
                if memory.id not in planned_superseded
                and memory.id not in blocked
            ]
            if dry_run:
                # Nothing was superseded for real, so the plan must skip the ids it would have merged.
                remaining = [memory for memory in remaining if memory.superseded_by_id is None]
            if {memory.id for memory in remaining} == before:
                # No progress (every merge this round was refused): another round would repeat it.
                break
        report.triage = totals

        # 2. Contradiction flags (non-destructive: report only).
        for a, b, overlap in self._find_contradictions(classified):
            report.contradictions.append(ContradictionCandidate(a, b, overlap))

        return report

    def _check_duplicate_groups(self, scope: Scope, groups: list[DuplicateGroup]) -> None:
        """OPS-06: pre-flight validation BEFORE any mutation per round."""
        for group in groups:
            survivor = self._repository.get_memory(scope, group.survivor_id)
            merged_sources = sorted(set(survivor.source_observation_ids) | set(group.merged_source_ids))
            for source_id in merged_sources:
                try:
                    self._repository.get_observation(scope, source_id)
                except NotFoundError as exc:
                    raise ValueError(
                        f"consolidation aborted before any write: survivor "
                        f"{survivor.id!r} references missing observation {source_id!r}"
                    ) from exc

    def _write_groups(
        self,
        scope: Scope,
        groups: list[DuplicateGroup],
        summaries: list[str],
        report: ConsolidationReport,
        *,
        dry_run: bool,
        planned_superseded: set[str],
    ) -> set[str]:
        """Apply one round of merge groups: supersede, audit, and one summary memory per group.

        Returns the ids whose supersede the guarded path refused (a lost race). A group whose merge
        did not fully apply writes NO summary: a memory saying "this group is consolidated" must not
        exist when it is not, and the ids are what a reader would follow.
        """
        blocked: set[str] = set()
        for group, summary in zip(groups, summaries, strict=True):
            survivor = self._repository.get_memory(scope, group.survivor_id)
            merged_sources = sorted(set(survivor.source_observation_ids) | set(group.merged_source_ids))
            if dry_run:
                report.planned.append(
                    {
                        "survivor_id": survivor.id,
                        "supersede": list(group.superseded_ids),
                        "similarity": group.similarity,
                        "marker": summary,
                    }
                )
                planned_superseded.update(group.superseded_ids)
            for old_id in group.superseded_ids:
                # OPS-05: idempotent re-run — a second consolidate pass on
                # the same scope must not crash on an already-superseded
                # duplicate (supersede_memory raises ConflictError). Skip
                # members already superseded by a previous pass.
                existing = self._repository.get_memory(scope, old_id)
                if existing.superseded_by_id is not None:
                    continue
                if dry_run:
                    continue
                try:
                    # M2's guarded path: ONE conditional UPDATE, ConflictError on a lost race.
                    self._repository.supersede_memory(scope, old_id, replacement_id=survivor.id)
                except ConflictError as exc:
                    # Owner decision 7: a lost race is "skip and log", never a failed pass.
                    report.skipped_supersedes += 1
                    blocked.add(old_id)
                    self._repository.append_audit(
                        scope,
                        action="dreaming.skip",
                        target_id=old_id,
                        metadata_={"reason": str(exc), "replacement_id": survivor.id},
                    )
                    report.audit_events += 1
                    continue
                self._repository.append_audit(
                    scope,
                    action="dreaming.merge",
                    target_id=old_id,
                    metadata_={"replacement_id": survivor.id, "similarity": group.similarity},
                )
                report.audit_events += 1
            # Emit a consolidated memory linking the group (M2's in-transaction audit-only helper).
            # Not when its merge was refused: a summary that names ids it did not merge is a lie.
            if not dry_run and not blocked.intersection(group.superseded_ids):
                consolidated, _row = self._repository.add_memory_with_audit(
                    Memory(
                        scope=scope,
                        kind="consolidated",
                        content=summary,
                        source_observation_ids=list(merged_sources or survivor.source_observation_ids),
                        model_id=self._model_id,
                        prompt_version=self._prompt_version,
                        confidence=group.similarity,
                    ),
                    action="dreaming.consolidate",
                    metadata_={
                        "survivor_id": survivor.id,
                        "memory_ids": sorted(group.all_ids),
                        "similarity": group.similarity,
                        "writer": "model" if self._writer is not None else "deterministic",
                    },
                )
                report.consolidated_memory_ids.append(consolidated.id)
                report.audit_events += 1
            report.duplicate_groups.append(group)
        return blocked

    def _default_summary(self, group: DuplicateGroup, survivor: Memory) -> str:
        """The deterministic merge summary: the group's first words and the ids it unifies."""
        return (
            f"Consolidated memory group: {survivor.content[:80]!r} "
            f"unifies {len(group.all_ids)} memories "
            f"(ids: {', '.join(sorted(group.all_ids)[:5])})"
        )

    def _summaries(
        self,
        memories: list[Memory],
        groups: list[DuplicateGroup],
        report: ConsolidationReport,
        *,
        dry_run: bool = False,
    ) -> list[str]:
        """One summary per group: the writer's sentence where it produced one, else the default text.

        The writer is given the group's own words (never ids) in batches of 8 and may only return
        a sentence; anything it does not return falls back to :meth:`_default_summary`, so a broken
        or absent model cannot change what the pass decides or lose the record of a merge.
        """
        by_id = {memory.id: memory for memory in memories}
        defaults = [self._default_summary(group, by_id[group.survivor_id]) for group in groups]
        if dry_run:
            # A dry run is free: it shows the deterministic summary a confirmed run would store when
            # no writer is configured, and never spends a call to show what it WOULD have written.
            return defaults
        if self._writer is None or not groups:
            if self._writer is not None:
                report.writer = self._writer.stats.summary()
            return defaults
        inputs = [by_id[group.survivor_id].content for group in groups]
        written = self._writer.write(inputs)
        report.writer = self._writer.stats.summary()
        return [
            text if text else default
            for text, default in zip(written, defaults, strict=True)
        ]

    def _find_duplicate_groups(self, memories: list[Memory], classified: Triage | None = None) -> list[DuplicateGroup]:
        """Group near-duplicates, newest (created_at, then id) survives.

        Uses MinHash-LSH as a candidate generator (same signatures as
        contradiction detection), with exact Jaccard as the final merge
        decision. Deterministic grouping/survivor semantics preserved:
        ordered by (created_at, id), first match wins, later memory survives.

        The decision itself is the triage verdict (``dreamtriage.DUPLICATE``), so a pass has one
        classifier rather than a merge rule here and a contradiction rule next to it.
        """
        classified = classified if classified is not None else triage(
            memories,
            duplicate_jaccard=self._duplicate_jaccard,
            contradiction_jaccard=self._contradiction_jaccard,
        )
        duplicates = {
            frozenset((verdict.memory_a_id, verdict.memory_b_id)): verdict for verdict in classified.duplicates
        }
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
                verdict = duplicates.get(frozenset((mem_a.id, mem_b.id)))
                if verdict is not None:
                    sim = verdict.overlap
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

    def _find_contradictions(self, classified: Triage) -> list[tuple[str, str, float]]:
        """Pairs the triage called contradictions (same surface, a value-bearing difference).

        Report only. A restatement (same fact, different words) is NOT a contradiction and is not
        listed here: the OPS rule is to hide only when certain, and "hide" is not something this
        pass does at all — it proposes.
        """
        return [(verdict.memory_a_id, verdict.memory_b_id, verdict.overlap) for verdict in classified.contradictions]


class CurationService:
    """Self-curation: salience scoring + non-destructive proposals."""

    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        salience_halflife_h: float = _SALIENCE_HALFLIFE_H,
        confidence_weight: float = _CONFIDENCE_WEIGHT,
        recency_weight: float = _RECENCY_WEIGHT,
        low_value_threshold: float = 0.15,
        model_id: str = "severian-curation-v1",
        prompt_version: str = "deterministic-salience-v1",
        now: datetime | None = None,
    ) -> None:
        self._repository = repository
        self._halflife = salience_halflife_h
        self._cw = confidence_weight
        self._rw = recency_weight
        self._low_value_threshold = low_value_threshold
        self._model_id = model_id
        self._prompt_version = prompt_version
        self._now = now or datetime.now(UTC)

    def curate(
        self,
        scope: Scope,
        *,
        exclude_kinds: Collection[str] = (),
        dry_run: bool = False,
    ) -> CurationReport:
        """Score every active memory and propose (never delete) actions.

        ``exclude_kinds`` keeps kinds out of the input. The dreaming pass excludes its own markers:
        a ``curation_review`` marker would otherwise score itself, its counts would change on every
        pass, and each pass would write another marker — growth instead of convergence.
        ``dry_run`` scores and proposes without writing the marker or the report row.
        """
        memories = _dream_input(self._repository, scope, exclude_kinds)
        report = CurationReport(scope=scope, dry_run=dry_run)
        for memory in memories:
            age_h = max((self._now - memory.created_at).total_seconds() / 3600.0, 0.0)
            recency = 0.5 ** (age_h / self._halflife)
            # No usage term: see the note on ``_RECENCY_WEIGHT``.
            salience = self._cw * memory.confidence + self._rw * recency
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
        # Contradiction candidates from the same deterministic triage the consolidation pass uses:
        # one classifier, so "restate" is never reported as a disagreement.
        classified = triage(memories)
        report.triage = classified.counts()
        for verdict in classified.contradictions:
            report.contradiction_ids.append((verdict.memory_a_id, verdict.memory_b_id))
        # Write ONE review-marker memory per scope summarising the pass.
        if report.scored and not dry_run:
            marker, _reference = self._repository.add_memory_with_audit(
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
                ),
                # The marker's text promises "review proposals in audit"; that promise
                # is only true if the report reference is persisted WITH the marker, in
                # one transaction (M2). Ids and counts only — never memory content.
                action="curation.report",
                metadata_=report.reference(),
            )
            report.review_marker_ids.append(marker.id)
        return report
