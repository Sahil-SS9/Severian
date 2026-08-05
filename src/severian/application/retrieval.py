from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from severian.application.ports import CanonicalRepository
from severian.application.projections import VectorProjection
from severian.domain.models import Memory, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.vector import _STOPWORDS

# Tokenize exactly like the hash embedding (and thus the FTS porter
# tokenizer) so coverage is computed on the same token space.
_TOKEN_RE = re.compile(r"[\w]+", flags=re.UNICODE)


def query_tokens(text: str) -> list[str]:
    """Lowercase, stopword-filtered, porter-stemmed tokens.

    Mirrors HashEmbedding.encode's tokenization so coverage is computed
    in the same space the vector and FTS projections match on.
    """
    from severian.infrastructure.stemming import porter_stem

    tokens: list[str] = []
    for token in _TOKEN_RE.findall(text.casefold()):
        if token in _STOPWORDS:
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
class RetrievalResult:
    memory: Memory
    source_observation_ids: list[str]
    lexical_score: float
    vector_score: float
    fused_score: float


class RetrievalService:
    def __init__(self, repository: CanonicalRepository, lexical: FTS5Index, vector: VectorProjection):
        self.repository = repository
        self.lexical = lexical
        self.vector = vector

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
    ) -> list[RetrievalResult]:
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
        if not FTS5Index.is_searchable(query):
            return []
        lexical_hits = self.lexical.search(scope, query, limit=max(limit, 50))
        vector_hits = self.vector.search(scope, query, limit=max(limit, 50))
        lexical_by_id = {hit.memory_id: hit for hit in lexical_hits}
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
        ids = set(lexical_by_id) | set(vector_by_id)
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
            fused = (1.0 / (60 + lexical_rank) if lexical_rank else 0.0) + (
                1.0 / (60 + vector_rank) if vector_rank else 0.0
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
            ranked = [
                item
                for item in ranked
                if item.lexical_score > 0.0 or item.fused_score >= min_fused_score
            ]
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
        return ranked[:limit]

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

    def assemble(self, results: list[RetrievalResult]) -> str:
        blocks: list[str] = []
        used = 0
        for result in results:
            citation = ", ".join(result.source_observation_ids)
            prefix = f"[{result.memory.id}] "
            suffix = f"\nsource: {citation}"
            available = self.max_chars - used - len(prefix) - len(suffix) - 2
            if available <= 0:
                break
            content = result.memory.content[:available]
            block = f"{prefix}{content}{suffix}"
            blocks.append(block)
            used += len(block) + 2
        return "\n\n".join(blocks)
