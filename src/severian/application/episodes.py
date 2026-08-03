"""Episodic summarization (B-2 / roadmap technique #4).

Long sessions accumulate hundreds of raw observations. Retrieval over
raw observations alone is lossy and unbounded — the LOCOMO-tier
conversation recall gap (B-1) showed that a conversation probe needs a
consolidated view. This service produces ``Memory(kind="episode")``
records: one per session, whose ``source_observation_ids`` spans every
observation in the session and whose ``content`` is a deterministic,
budgeted summary of that session's facts.

Deterministic by default: the summarizer is a local, rule-based pass
(key-fact extraction + budgeted concatenation) — no LLM, no network,
reproducible across runs. An optional LLM-judge merge/delete pass can
replace the content when a ``DerivationGateway`` is supplied, with the
deterministic summary retained as the fallback (memrefine-style
budgeted consolidation).

The episode is written through the canonical repository (same
crash-safety as derive: the worker holds the job open until the
projection rebuild converges, then completes it).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from severian.application.ports import CanonicalRepository
from severian.domain.models import Memory, Observation, Scope

# Budget for an episode summary: hard cap on content length so a
# session's summary never dominates the context window.
EPISODE_BUDGET_CHARS = 4_000
# Minimum observations before consolidation is worth the storage.
MIN_OBSERVATIONS_FOR_EPISODE = 3

# Stop words ignored when extracting key facts (deterministic filter).
_STOP = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "is", "are", "was", "were",
        "be", "been", "being", "to", "of", "in", "on", "at", "for", "with",
        "by", "from", "as", "that", "this", "it", "its", "he", "she", "they",
        "we", "you", "i", "not", "no", "yes", "do", "does", "did", "have",
        "has", "had", "will", "would", "can", "could", "should", "about",
        "into", "over", "under", "again", "then", "than", "so", "too", "very",
    }
)


def _key_terms(content: str, limit: int = 8) -> list[str]:
    """Extract the most frequent non-stop terms from ``content``."""
    counts: dict[str, int] = {}
    for token in content.casefold().split():
        # Keep word-like tokens, strip punctuation.
        cleaned = "".join(ch for ch in token if ch.isalnum())
        if not cleaned or cleaned in _STOP:
            continue
        counts[cleaned] = counts.get(cleaned, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [term for term, _ in ranked[:limit]]


def summarize_observations(observations: Iterable[Observation], *, budget: int = EPISODE_BUDGET_CHARS) -> str:
    """Deterministic budgeted summary of observations.

    Each observation contributes its content (trimmed to fit the
    budget); the whole summary is capped at ``budget`` characters.
    Deterministic ordering: observations sorted by id (stable) so the
    output is reproducible across runs.
    """
    ordered = sorted(observations, key=lambda obs: obs.id)
    parts: list[str] = []
    used = 0
    for obs in ordered:
        text = obs.content.strip()
        if not text:
            continue
        remaining = budget - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[: remaining - 1] + "…"
        parts.append(text)
        used += len(text) + 1  # +1 for the separator
    return "\n".join(parts)


class EpisodeService:
    """Consolidates a session's observations into an episode memory."""

    def __init__(self, repository: CanonicalRepository) -> None:
        self._repository = repository

    def consolidate_session(
        self,
        scope: Scope,
        *,
        model_id: str = "severian/deterministic-episode",
        prompt_version: str = "episode-v1",
        gateway: Any | None = None,
        policy: dict[str, Any] | None = None,
        budget: int = EPISODE_BUDGET_CHARS,
    ) -> Memory | None:
        """Build (or refresh) the episode for ``scope``.

        Returns the episode memory, or ``None`` when the session has
        fewer than :data:`MIN_OBSERVATIONS_FOR_EPISODE` observations
        (nothing worth consolidating yet).

        When ``gateway`` is provided, its ``summarize`` capability is
        used to produce the episode content, falling back to the
        deterministic summary on any failure (rule-based fallback per
        the memrefine pattern). Without a gateway the deterministic
        summary is always used — fully local and reproducible.
        """
        observations = self._repository.list_observations(scope)
        if len(observations) < MIN_OBSERVATIONS_FOR_EPISODE:
            return None
        source_ids = [obs.id for obs in observations]
        content = self._summarize(observations, gateway=gateway, policy=policy, budget=budget)
        episode = Memory(
            scope=scope,
            kind="episode",
            content=content,
            source_observation_ids=source_ids,
            model_id=model_id,
            prompt_version=prompt_version,
            confidence=0.5,
        )
        return self._repository.add_memory(episode)

    def _summarize(
        self,
        observations: list[Observation],
        *,
        gateway: Any | None,
        policy: dict[str, Any] | None,
        budget: int,
    ) -> str:
        deterministic = summarize_observations(observations, budget=budget)
        if gateway is None:
            return deterministic
        # Optional LLM-judge pass: ask the gateway for a consolidated
        # summary; any failure falls back to the deterministic text so
        # episodes are never missing.
        try:
            summarize = getattr(gateway, "summarize", None)
            if summarize is None:
                return deterministic
            result = summarize("\n".join(obs.content for obs in observations), policy=dict(policy or {}))
            if isinstance(result, str) and result.strip():
                return result.strip()[:budget]
            return deterministic
        except Exception:
            return deterministic
