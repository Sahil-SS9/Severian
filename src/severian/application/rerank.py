"""Rerank (Round 2 improvement 4): re-score the top search candidates with a cross-encoder.

Switched on by ``SEVERIAN_RERANK`` naming a cross-encoder (model name or local path); unset means search is
exactly as before. Only the top ``RERANK_POOL`` candidates are re-scored, ties keep their old order, and any
scorer failure (or a reply of the wrong length) falls back to the original order, so the reranker can only
reorder, never lose or add results. sentence-transformers is optional and imported only when switched on.
"""

from __future__ import annotations

import functools
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


def _positive_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        if raw:
            _LOG.warning("%s=%r is not a whole number; using the default", name, raw)
        return None
    if value < 1:
        _LOG.warning("%s=%r must be at least 1; using the default", name, raw)
        return None
    return value


def rerank_pool() -> int:
    """``SEVERIAN_RERANK_POOL``: how many top candidates are re-scored (default ``RERANK_POOL``)."""
    return _positive_int("SEVERIAN_RERANK_POOL") or RERANK_POOL


def rerank_max_tokens() -> int | None:
    """``SEVERIAN_RERANK_MAX_TOKENS``: cut each candidate to this many model tokens (None = the model's own limit)."""
    return _positive_int("SEVERIAN_RERANK_MAX_TOKENS")


def reranked(
    query: str, items: Sequence[T], scorer: Reranker, *, text: Callable[[T], str], pool: int = RERANK_POOL
) -> list[T]:
    """``items`` with the top ``pool`` re-ordered by ``scorer``; the tail keeps its place."""
    head, tail = list(items[:pool]), list(items[pool:])
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
    """A loaded sentence-transformers cross-encoder."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self._model.predict([(query, t) for t in texts])]


def rerank_device() -> str | None:
    """``SEVERIAN_RERANK_DEVICE`` (``cuda:1``, ``cpu``, or ``sidecar`` = the embedding sidecar's shared model)."""
    return os.environ.get("SEVERIAN_RERANK_DEVICE", "").strip() or None


def _load(model: str, device: str | None, max_tokens: int | None = None) -> Any:
    from sentence_transformers import CrossEncoder  # optional dependency

    return CrossEncoderReranker(CrossEncoder(model, device=device, max_length=max_tokens))


@functools.lru_cache(maxsize=4)
def load_reranker(model: str, device: str | None, max_tokens: int | None = None) -> Any | None:
    """Load once per process. The device fails: try the CPU. Nothing loads: None (no rerank). Never raises."""
    if device == "sidecar":
        socket_path = os.environ.get("SEVERIAN_EMBED_SOCKET", "").strip()
        if not socket_path:
            _LOG.warning("SEVERIAN_RERANK_DEVICE=sidecar needs SEVERIAN_EMBED_SOCKET; searching without rerank")
            return None
        from severian.infrastructure.remote_qwen_embedding import RemoteReranker

        return RemoteReranker(socket_path)
    try:
        return _load(model, device, max_tokens)
    except Exception as exc:  # noqa: BLE001 - loading a reranker must never break Severian
        if device != "cpu":
            _LOG.warning("rerank model %s failed to load on %s (%s); falling back to the CPU", model, device, exc)
            try:
                return _load(model, "cpu", max_tokens)
            except Exception as cpu_exc:  # noqa: BLE001
                exc = cpu_exc
        _LOG.warning("rerank model %s failed to load (%s); searching without rerank", model, exc)
        return None
