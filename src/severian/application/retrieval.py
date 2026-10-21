from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from severian.application.eventtime import TimeWindow, as_utc, message_time
from severian.application.ports import CanonicalRepository, LexicalIndex
from severian.application.projections import VectorProjection
from severian.application.replytrim import fit_contents
from severian.domain.errors import NotFoundError
from severian.domain.models import Memory, Scope
from severian.domain.text import STOPWORDS, porter_stem

# Tokenize exactly like the hash embedding (and thus the FTS porter
# tokenizer) so coverage is computed on the same token space.
_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)


def query_tokens(text: str) -> list[str]:
    """Lowercase, stopword-filtered, porter-stemmed tokens.

    Mirrors HashEmbedding.encode's tokenization so coverage is computed
    in the same space the vector and FTS projections match on.
    """
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(text.casefold()):
        if token in STOPWORDS:
            continue
        tokens.append(porter_stem(token))
    return tokens


def query_coverage(query: str, content: str) -> float:
    """Fraction of query tokens present in content (0.0..1.0).

    Genuine evidence shares 2+ meaningful (stemmed) tokens with the
    query; lexical-overlap decoys share 1 or none (e.g. 'new' in
    'the new CFO'). Used as the abstention signal: a top hit with
    coverage below the floor has no real support and the query should
    abstain (return no evidence) rather than answer from noise.
    """
    q_tokens = query_tokens(query)
    if not q_tokens:
        return 0.0
    c_tokens = set(query_tokens(content))
    if not c_tokens:
        return 0.0
    return sum(1 for token in q_tokens if token in c_tokens) / len(q_tokens)


@dataclass(frozen=True)
class SearchTuning:
    """Step 2 search knobs. The defaults reproduce the pre-tuning behaviour exactly.

    ``fusion_k`` / ``lexical_weight`` / ``vector_weight``: weighted reciprocal-rank fusion,
    ``lexical_weight / (k + lexical_rank) + vector_weight / (k + vector_rank)``.
    ``neighbours`` / ``neighbour_weight``: when a memory matches, also consider the memories
    from the ``neighbours`` turns either side of it in the same source conversation, scored at
    ``neighbour_weight`` times the match. 0 turns = off.
    """

    fusion_k: int = 60
    lexical_weight: float = 1.0
    vector_weight: float = 1.0
    neighbours: int = 0
    neighbour_weight: float = 0.5
    neighbour_seeds: int = 5
    # Step 3: score multiplier for a memory whose event time falls in the period the question names.
    time_boost: float = 2.0
    # Step 4: weight of the "when this will be needed" cue channel in fusion. 0 = cues not searched.
    cue_weight: float = 0.0

    def __post_init__(self) -> None:
        if self.fusion_k < 1:
            raise ValueError("fusion_k must be at least 1")
        weights = (self.lexical_weight, self.vector_weight, self.neighbour_weight, self.cue_weight)
        if min(weights) < 0 or self.time_boost < 1:
            raise ValueError("search weights must not be negative and time_boost must be at least 1")
        if self.neighbours < 0 or self.neighbour_seeds < 1:
            raise ValueError("neighbours must not be negative and neighbour_seeds must be at least 1")

    @classmethod
    def from_env(cls) -> SearchTuning:
        """Read SEVERIAN_FUSION_K|LEXICAL|VECTOR and SEVERIAN_NEIGHBOURS, SEVERIAN_NEIGHBOUR_WEIGHT|SEEDS."""
        env = os.environ
        defaults = cls()
        return cls(
            fusion_k=int(env.get("SEVERIAN_FUSION_K", defaults.fusion_k)),
            lexical_weight=float(env.get("SEVERIAN_FUSION_LEXICAL", defaults.lexical_weight)),
            vector_weight=float(env.get("SEVERIAN_FUSION_VECTOR", defaults.vector_weight)),
            neighbours=int(env.get("SEVERIAN_NEIGHBOURS", defaults.neighbours)),
            neighbour_weight=float(env.get("SEVERIAN_NEIGHBOUR_WEIGHT", defaults.neighbour_weight)),
            neighbour_seeds=int(env.get("SEVERIAN_NEIGHBOUR_SEEDS", defaults.neighbour_seeds)),
            time_boost=float(env.get("SEVERIAN_TIME_BOOST", defaults.time_boost)),
            cue_weight=float(env.get("SEVERIAN_FUSION_CUE", defaults.cue_weight)),
        )


@dataclass(frozen=True)
class RetrievalResult:
    memory: Memory
    source_observation_ids: list[str]
    lexical_score: float
    vector_score: float
    fused_score: float


class RetrievalService:
    def __init__(
        self,
        repository: CanonicalRepository,
        lexical: LexicalIndex,
        vector: VectorProjection,
        tuning: SearchTuning | None = None,
    ):
        self.repository = repository
        self.lexical = lexical
        self.vector = vector
        self.tuning = tuning if tuning is not None else SearchTuning.from_env()

    def search(
        self,
        scope: Scope,
        query: str,
        *,
        limit: int = 20,
        min_vector_score: float | None = None,
        min_fused_score: float | None = None,
        min_coverage: float | None = None,
        salience_weight: float = 0.0,
        salience_halflife_h: float = 720.0,
        now: datetime | None = None,
        as_of: datetime | None = None,
        window: TimeWindow | None = None,
    ) -> list[RetrievalResult]:
        """Hybrid search. ``as_of`` keeps only memories known by then; ``window`` boosts memories from that period."""
        stripped = query.strip()
        # Recall-all path: the admin graph (and CLI `list`-style flows)
        # need "everything in this scope", not a ranked query. Empty and
        # `*` queries mean "list all memories" — returning every memory
        # in scope with neutral scores.
        if not stripped or stripped == "*":
            memories = self.repository.list_memories(scope)
            return [
                RetrievalResult(
                    memory=memory,
                    source_observation_ids=list(memory.source_observation_ids),
                    lexical_score=0.0,
                    vector_score=0.0,
                    fused_score=0.0,
                )
                for memory in memories
            ][:limit]
        if not self.lexical.is_searchable(query):
            return []
        lexical_hits = self.lexical.search(scope, query, limit=max(limit, 50))
        vector_hits = self.vector.search(scope, query, limit=max(limit, 50))
        lexical_by_id = {hit.memory_id: hit for hit in lexical_hits}
        # Step 4 cue channel: memories whose "when needed" cues match the question. Off at weight 0.
        cue_hits = (
            self.lexical.search_cues(scope, query, limit=max(limit, 50))
            if self.tuning.cue_weight > 0 and hasattr(self.lexical, "search_cues")
            else []
        )
        cue_rank_by_id = {hit.memory_id: position for position, hit in enumerate(cue_hits, start=1)}
        vector_by_id = {hit.memory_id: hit for hit in vector_hits}
        # Dedupe by content: the correction/derive path can leave TWO
        # memories with IDENTICAL content (the revision memory + the
        # re-derived memory from the superseding observation). They tie
        # on every content-based key (FTS rank, vector score, content),
        # so their relative order falls to the random memory UUID —
        # which differs across fresh adapters and makes fused RANKS
        # (and therefore fused scores) non-deterministic. Since the
        # duplicates are the same fact, keep one per content (the
        # lowest memory_id — content-identical, so any pick is
        # semantically equal and the choice does not affect scores).
        tuning = self.tuning
        ids = set(lexical_by_id) | set(vector_by_id) | set(cue_rank_by_id)
        ranked: list[RetrievalResult] = []
        for memory_id in ids:
            memory = self.repository.get_memory(scope, memory_id)
            lexical_rank = next(
                (position for position, hit in enumerate(lexical_hits, start=1) if hit.memory_id == memory_id),
                None,
            )
            vector_rank = next(
                (position for position, hit in enumerate(vector_hits, start=1) if hit.memory_id == memory_id),
                None,
            )
            cue_rank = cue_rank_by_id.get(memory_id)
            fused = (
                (tuning.cue_weight / (tuning.fusion_k + cue_rank) if cue_rank else 0.0)
                + (tuning.lexical_weight / (tuning.fusion_k + lexical_rank) if lexical_rank else 0.0)
                + (tuning.vector_weight / (tuning.fusion_k + vector_rank) if vector_rank else 0.0)
            )
            # Relevance floor: a vector-only hit below the floor is noise
            # (hash embeddings always produce SOME positive dot product).
            # Lexical hits (token matches) always pass — BM25 does not
            # return non-matching rows.
            vector_score = vector_by_id[memory_id].score if memory_id in vector_by_id else 0.0
            lexical_score = lexical_by_id[memory_id].score if memory_id in lexical_by_id else 0.0
            if (
                min_vector_score is not None
                and lexical_rank is None
                and cue_rank is None
                and vector_score < min_vector_score
            ):
                continue
            ranked.append(
                RetrievalResult(
                    memory=memory,
                    source_observation_ids=list(memory.source_observation_ids),
                    lexical_score=lexical_score,
                    vector_score=vector_score,
                    fused_score=fused,
                )
            )
        # Total-order tiebreak: identical fused scores resolve by content
        # (the stable key across runs), then by memory UUID as the final
        # total-order safety net. Content is unique for distinct facts.
        ranked.sort(key=lambda item: (-item.fused_score, item.memory.content, item.memory.id))
        if as_of is not None:
            ranked = [item for item in ranked if self._known_by(scope, item.memory, as_of)]
        if window is not None and tuning.time_boost > 1.0:
            ranked = self._boost_window(ranked, window)
        # Salience-weighted ranking (Tier 2, opt-in): when salience_weight
        # > 0, boost memories that are recent and/or frequently used. The
        # salience factor is deterministic (computed from created_at +
        # usage_count, never from random state). Default 0.0 = unchanged
        # pure-relevance behaviour (backwards compatible, benchmark-safe).
        if salience_weight > 0.0:
            now_value = now or datetime.now(UTC)
            weighted: list[tuple[float, RetrievalResult]] = []
            for item in ranked:
                age_h = max((now_value - item.memory.created_at).total_seconds() / 3600.0, 0.0)
                recency = 0.5 ** (age_h / salience_halflife_h)
                usage = min(item.memory.usage_count / 100.0, 1.0)  # saturate at 100 hits
                salience = 0.5 * recency + 0.5 * usage
                adjusted = item.fused_score * (1.0 + salience_weight * salience)
                weighted.append((adjusted, item))
            weighted.sort(key=lambda pair: (-pair[0], pair[1].memory.content))
            ranked = [item for _adjusted, item in weighted]
        # Abstention floor: a query whose best fused score is below the
        # floor returns NOTHING — the honest "no good match" answer. This
        # is what stops lexical token-overlap (e.g. "annual revenue"
        # matching a doc that mentions revenue once) from fabricating
        # answers to unanswerable probes (LOCOMO q9/q11 class).
        # The floor applies ONLY to hits with no lexical component
        # (pure hash-embedding noise): a hit with a real token match is
        # genuine evidence and must survive even at low fused score
        # (LOCOMO q7: weak-but-genuine lexical match).
        if min_fused_score is not None:
            ranked = [item for item in ranked if item.lexical_score > 0.0 or item.fused_score >= min_fused_score]
        # Query-coverage abstention: when enabled, EVERY returned hit must
        # cover a minimum fraction of the query's meaningful (stemmed,
        # non-stopword) tokens. A lexical decoy can share 1 generic token
        # with the query ("project" in "Project Chrysalis" vs a stale
        # "project lead" doc) yet have zero real support; a genuine hit
        # shares 2+ query tokens. If the best candidate fails the floor
        # the query ABSTAINS entirely (returns no evidence); weaker
        # candidates that fail the floor are dropped individually. This
        # is the principled replacement for an absolute fused-score
        # threshold, which cannot separate decoys from genuine weak
        # matches (their fused-score ranges overlap).
        if min_coverage is not None:
            filtered: list[RetrievalResult] = []
            for item in ranked:
                if query_coverage(query, item.memory.content) >= min_coverage:
                    filtered.append(item)
            if filtered != ranked:
                ranked = filtered
            if not ranked:
                return []
        if tuning.neighbours:
            ranked = self._with_neighbours(scope, ranked, limit=limit, as_of=as_of)
        return ranked[:limit]

    def _known_by(self, scope: Scope, memory: Memory, as_of: datetime) -> bool:
        """True when the memory's source message was written by ``as_of`` (message time, else save time)."""
        moments = []
        for observation_id in memory.source_observation_ids:
            try:
                observation = self.repository.get_observation(scope, observation_id)
            except NotFoundError:
                continue
            moments.append(message_time(observation.source, observation.created_at))
        known_at = min(moments) if moments else memory.created_at
        return known_at <= as_utc(as_of)

    def _boost_window(self, ranked: list[RetrievalResult], window: TimeWindow) -> list[RetrievalResult]:
        """Lift memories whose event time overlaps the period the question names; others are untouched."""
        boosted = [
            replace(item, fused_score=item.fused_score * self.tuning.time_boost)
            if item.memory.occurred_at is not None
            and window.overlaps(item.memory.occurred_at, item.memory.occurred_until or item.memory.occurred_at)
            else item
            for item in ranked
        ]
        boosted.sort(key=lambda item: (-item.fused_score, item.memory.content, item.memory.id))
        return boosted

    def _with_neighbours(
        self, scope: Scope, ranked: list[RetrievalResult], *, limit: int, as_of: datetime | None = None
    ) -> list[RetrievalResult]:
        """Add the conversation neighbours of the best matches (MemMachine "nucleus + neighbours").

        A neighbour is a memory derived from a turn within ``neighbours`` positions of a matched
        memory's turn, in the same source session. It is scored below its match so a real match
        still ranks first, and a neighbour that already matched keeps its own (higher) score.
        Reads only what it needs: the matched conversations' turns, then the memories of the
        neighbouring turns (two bounded queries; it used to read the whole scope per search).
        """
        tuning = self.tuning
        if not ranked:
            return ranked
        seed_observation_ids = {
            oid for seed in ranked[: tuning.neighbour_seeds] for oid in seed.memory.source_observation_ids
        }
        seed_sessions: set[str] = set()
        for observation_id in sorted(seed_observation_ids):
            try:
                observation = self.repository.get_observation(scope, observation_id)
            except LookupError:
                continue
            session = str(observation.source.get("session_id", "")).strip()
            if session:
                seed_sessions.add(session)
        if not seed_sessions:
            return ranked
        sessions: dict[str, list[tuple[datetime, str]]] = {}
        position: dict[str, tuple[str, int]] = {}
        for observation in self.repository.list_session_observations(scope, sorted(seed_sessions)):
            session = str(observation.source.get("session_id", "")).strip()
            if session:
                sessions.setdefault(session, []).append((observation.created_at, observation.id))
        for session, turns in sessions.items():
            turns.sort()
            for index, (_order, observation_id) in enumerate(turns):
                position[observation_id] = (session, index)
        window_ids: set[str] = set()
        for observation_id in seed_observation_ids:
            place = position.get(observation_id)
            if place is not None:
                session, index = place
                turns = sessions[session]
                low, high = max(0, index - tuning.neighbours), min(len(turns), index + tuning.neighbours + 1)
                window_ids.update(neighbour_id for _order, neighbour_id in turns[low:high])
        memories_by_observation: dict[str, list[Memory]] = {}
        for memory in self.repository.list_memories_for_observations(scope, sorted(window_ids)):
            for observation_id in memory.source_observation_ids:
                memories_by_observation.setdefault(observation_id, []).append(memory)
        present = {item.memory.id for item in ranked}
        added: list[RetrievalResult] = []
        for seed in ranked[: tuning.neighbour_seeds]:
            for observation_id in seed.memory.source_observation_ids:
                place = position.get(observation_id)
                if place is None:
                    continue
                session, index = place
                turns = sessions[session]
                low, high = max(0, index - tuning.neighbours), min(len(turns), index + tuning.neighbours + 1)
                for _order, neighbour_id in turns[low:high]:
                    for memory in memories_by_observation.get(neighbour_id, ()):
                        if memory.id in present or (as_of is not None and not self._known_by(scope, memory, as_of)):
                            continue
                        present.add(memory.id)
                        added.append(
                            RetrievalResult(
                                memory=memory,
                                source_observation_ids=list(memory.source_observation_ids),
                                lexical_score=0.0,
                                vector_score=0.0,
                                fused_score=seed.fused_score * tuning.neighbour_weight,
                            )
                        )
        if not added:
            return ranked
        merged = [*ranked, *added]
        merged.sort(key=lambda item: (-item.fused_score, item.memory.content, item.memory.id))
        return merged

    def search_shared(
        self,
        scope: Scope,
        agent_ids: list[str],
        query: str,
        *,
        limit: int = 20,
        min_vector_score: float | None = None,
        min_fused_score: float | None = None,
        min_coverage: float | None = None,
        salience_weight: float = 0.0,
        salience_halflife_h: float = 720.0,
        now: datetime | None = None,
    ) -> list[RetrievalResult]:
        """Shared-scope retrieval across a set of agent identities (Tier 3-3).

        Multi-agent shared read: queries each agent's exact 5-D scope
        independently (preserving the isolation boundary — the projections
        are per-scope and never crossed), then MERGES the results
        deterministically.

        Semantics:
        - the base ``scope`` supplies tenant/profile/collection/session;
        - ``agent_ids`` lists the agents whose private memory is shared
          into this read (the caller's visibility rule);
        - results are deduplicated by memory id and re-ranked by fused
          score (tiebreak by content, deterministic);
        - ONLY the caller's own scope + the listed agents are touched —
          no wildcard, no cross-tenant access.

        This is the explicit, opt-in shared-memory primitive the concept
        map listed as MISSING: two agents can read each other's memories
        WITHOUT collapsing identity, and the sharing is caller-declared
        (the visibility rule lives at the call site, not in the store).
        """
        if not agent_ids:
            raise ValueError("search_shared requires at least one agent_id")
        all_results: dict[str, RetrievalResult] = {}
        scopes = [scope]
        for agent_id in agent_ids:
            scopes.append(scope.model_copy(update={"agent_id": agent_id}))
        for candidate_scope in scopes:
            hits = self.search(
                candidate_scope,
                query,
                limit=max(limit, 50),
                min_vector_score=min_vector_score,
                min_fused_score=min_fused_score,
                min_coverage=min_coverage,
                salience_weight=salience_weight,
                salience_halflife_h=salience_halflife_h,
                now=now,
            )
            for hit in hits:
                all_results.setdefault(hit.memory.id, hit)
        ranked = sorted(
            all_results.values(),
            key=lambda item: (-item.fused_score, item.memory.content, item.memory.id),
        )
        return ranked[:limit]


class ContextAssembler:
    def __init__(self, max_chars: int = 8_000):
        if max_chars < 32:
            raise ValueError("max_chars must be at least 32")
        self.max_chars = max_chars

    def assemble(
        self,
        results: list[RetrievalResult],
        *,
        contents: Sequence[str] | None = None,
        query: str | None = None,
        trim_replies: bool = False,
        note: str | None = None,
    ) -> str:
        """Results as cited blocks within ``max_chars``: whole blocks in order, the first that does not fit cut short.

        ``contents`` replaces the text shown for each result (same order). ``trim_replies`` (fix a,
        SEVERIAN_REPLY_TRIM): when the blocks would not all fit, replies are dropped from the last saved turn upward
        until they do (``fit_contents``), before anything else is cut, unless ``query`` asks what was said. ``note``
        (fix b, SEVERIAN_CONFLICT_NOTES) goes after the blocks with its room kept free; it is left out when that room
        would leave the blocks under 32 characters. With none of these the output is exactly as
        before.
        """
        texts = [result.memory.content for result in results] if contents is None else list(contents)
        if len(texts) != len(results):
            raise ValueError("contents must give one text per result")
        limit = self.max_chars
        if note and limit - len(note) - 2 < 32:
            note = None
        if note:
            limit -= len(note) + 2
        frames = [
            (f"[{result.memory.id}] ", f"\nsource: {', '.join(result.source_observation_ids)}") for result in results
        ]
        if trim_replies:

            def cost(items: Sequence[str]) -> int:
                pairs = zip(frames, items, strict=True)
                return sum(len(prefix) + len(item) + len(suffix) + 2 for (prefix, suffix), item in pairs)

            texts = fit_contents(texts, limit, query=query, cost=cost)
        blocks = self._layout(frames, texts, limit)
        if note and blocks:
            blocks.append(note)
        return "\n\n".join(blocks)

    @staticmethod
    def _layout(frames: Sequence[tuple[str, str]], texts: Sequence[str], limit: int) -> list[str]:
        """Whole blocks in order within ``limit``, the first that does not fit cut short, then stop."""
        blocks: list[str] = []
        used = 0
        for (prefix, suffix), text in zip(frames, texts, strict=True):
            available = limit - used - len(prefix) - len(suffix) - 2
            if available <= 0:
                break
            block = f"{prefix}{text[:available]}{suffix}"
            blocks.append(block)
            used += len(block) + 2
        return blocks
