"""Qwen3-Embedding-0.6B semantic embedding provider.

This is the selected CPU-first semantic space: the immutable Qwen model
revision is projected through its Matryoshka representation to 384 dimensions.
Documents are encoded without an instruction; retrieval queries use the exact
instruction contract measured in the constrained bake-off.  The distinction is
part of the persisted vector-space fingerprint, so a Granite index cannot be
opened for writes with this provider.
"""
from __future__ import annotations

import logging
import math
import os
from pathlib import Path
from typing import Any

from severian.infrastructure.vector import HashEmbedding

logger = logging.getLogger(__name__)

QWEN_MODEL = "Qwen/Qwen3-Embedding-0.6B"
QWEN_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
QWEN_DIMENSION = 384
QWEN_RETRIEVAL_PROMPT = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:"
)


class QwenEmbedding:
    """Pinned Qwen embedding with a query-only retrieval instruction.

    Loading is lazy.  The central resolver never calls :meth:`or_hash`: a
    requested Qwen space must remain Qwen or fail visibly rather than silently
    putting hash vectors into a Qwen-indexed store.  ``or_hash`` remains an
    explicit opt-in escape hatch for callers that deliberately require a
    working 384-dimensional local fallback.
    """

    def __init__(
        self,
        model_name: str = QWEN_MODEL,
        *,
        revision: str = QWEN_REVISION,
        device: str | None = None,
    ) -> None:
        self.model_name = model_name
        self.revision = revision
        self.device = device
        self.dimension = QWEN_DIMENSION
        self.prompt_contract = QWEN_RETRIEVAL_PROMPT
        self._model: Any | None = None
        self._announced_failure = False

    @property
    def loaded(self) -> bool:
        """Whether the SentenceTransformers model is available in memory."""
        return self._model is not None

    def load(self) -> bool:
        """Load the pinned model and return whether it became available."""
        if self._model is not None:
            return True
        try:
            model_cache = (
                Path.home()
                / ".cache"
                / "huggingface"
                / "hub"
                / "models--Qwen--Qwen3-Embedding-0.6B"
            )
            if model_cache.is_dir():
                # Cached revision is immutable and should not make a network
                # request during service start-up.
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ["TQDM_DISABLE"] = "1"

            from sentence_transformers import SentenceTransformer

            kwargs: dict[str, object] = {"revision": self.revision}
            if self.device is not None:
                kwargs["device"] = self.device
            self._model = SentenceTransformer(self.model_name, **kwargs)
            logger.info("Qwen embeddings loaded (dim=%s, revision=%s)", self.dimension, self.revision[:12])
            return True
        except Exception as exc:  # pragma: no cover - dependency/cache dependent
            if not self._announced_failure:
                logger.warning(
                    "Qwen embeddings unavailable; explicit hash fallback remains available: %s", exc
                )
                self._announced_failure = True
            return False

    def _encode_contract(self, text: str, *, query: bool) -> list[float]:
        if self._model is None and not self.load():
            raise RuntimeError("Qwen embedding not loaded; call load() or explicitly use or_hash()")
        assert self._model is not None

        kwargs: dict[str, object] = {
            "normalize_embeddings": True,
            "show_progress_bar": False,
            "truncate_dim": self.dimension,
        }
        if query:
            kwargs["prompt"] = self.prompt_contract
        vector = self._model.encode(text, **kwargs)
        values = [float(value) for value in vector]
        if len(values) != self.dimension:
            raise ValueError(
                f"Qwen encoder returned {len(values)} dims, expected {self.dimension} "
                f"(truncate_dim={self.dimension})"
            )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Qwen encoded vector contains non-finite values")
        try:
            norm = math.sqrt(math.fsum(value * value for value in values))
        except OverflowError as exc:
            raise ValueError("Qwen encoded vector norm overflowed") from exc
        if not math.isfinite(norm):
            raise ValueError("Qwen encoded vector norm overflowed")
        if not norm > 0.0:
            raise ValueError("Qwen encoded vector has zero norm")
        normalised = [value / norm for value in values]
        verified_norm = math.sqrt(math.fsum(value * value for value in normalised))
        if not math.isclose(verified_norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(f"Qwen encoded vector is not normalised (norm={verified_norm:.6f})")
        return normalised

    def encode(self, text: str) -> list[float]:
        """Encode a document without a retrieval instruction."""
        return self._encode_contract(text, query=False)

    def encode_query(self, text: str) -> list[float]:
        """Encode a retrieval query with the exact selected instruction."""
        return self._encode_contract(text, query=True)

    def or_hash(self) -> QwenEmbedding | HashEmbedding:
        """Explicit 384-dimensional fallback for callers that request it."""
        if self.load():
            return self
        return HashEmbedding(dimension=self.dimension)


__all__ = [
    "QWEN_DIMENSION",
    "QWEN_MODEL",
    "QWEN_RETRIEVAL_PROMPT",
    "QWEN_REVISION",
    "QwenEmbedding",
]
