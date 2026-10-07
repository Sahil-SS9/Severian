"""Rerank (Round 2 improvement 4): re-score the top search candidates with a cross-encoder.

Switched on by ``SEVERIAN_RERANK`` naming a cross-encoder (model name or local path); unset means search is
exactly as before. Only the top ``RERANK_POOL`` candidates are re-scored, ties keep their old order, and any
scorer failure (or a reply of the wrong length) falls back to the original order, so the reranker can only
reorder, never lose or add results. sentence-transformers is optional and imported only when switched on.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any, Protocol, TypeVar

RERANK_POOL = 30
_LOG = logging.getLogger(__name__)
T = TypeVar("T")


class Reranker(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


def rerank_model() -> str | None:
    name = os.environ.get("SEVERIAN_RERANK", "").strip()
    return name or None


def reranked(query: str, items: Sequence[T], scorer: Reranker, *, text: Callable[[T], str]) -> list[T]:
    """``items`` with the top ``RERANK_POOL`` re-ordered by ``scorer``; the tail keeps its place."""
    head, tail = list(items[:RERANK_POOL]), list(items[RERANK_POOL:])
    if len(head) < 2:
        return list(items)
    try:
        scores = scorer.score(query, [text(item) for item in head])
    except Exception:  # noqa: BLE001 - a reranker must never break search
        _LOG.warning("rerank failed; keeping the original order", exc_info=True)
        return list(items)
    if len(scores) != len(head):
        _LOG.warning("rerank returned %d scores for %d candidates; keeping the original order", len(scores), len(head))
        return list(items)
    order = sorted(range(len(head)), key=lambda i: (-scores[i], i))
    return [head[i] for i in order] + tail


class CrossEncoderReranker:
    """A sentence-transformers cross-encoder, loaded once, on the GPU when one is free."""

    def __init__(self, model: str) -> None:
        from sentence_transformers import CrossEncoder  # optional dependency

        device = os.environ.get("SEVERIAN_RERANK_DEVICE") or None
        self._model: Any = CrossEncoder(model, device=device)

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._model.predict([(query, t) for t in texts])]
