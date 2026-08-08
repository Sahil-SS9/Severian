"""Granite R2 semantic embedding provider (A-1 / roadmap technique #1).

Pluggable neural embedding that satisfies :class:`EmbeddingProvider`
(dimension + encode). Uses IBM Granite Embedding (107M multilingual,
Apache-2.0, CPU-friendly) via the ``sentence-transformers`` interface
with ONNX runtime when available.

Design (locked in HANDOFF-MASTER and roadmap):
- **Optional dependency**: ``sentence-transformers`` / ``onnxruntime``
  are imported lazily. When unavailable, ``load()`` returns ``None`` and
  callers fall back to the deterministic :class:`HashEmbedding` — the
  product never hard-depends on a neural model.
- **Fallback guarantee**: :meth:`or_hash` returns a working embedding
  (the neural one if loaded, else HashEmbedding) so composition is
  always safe.
- **Dimension change is a storage migration**: the local vector index
  stores vectors as JSON arrays (any dimension); pgvector stores
  ``vector(N)`` columns where N is fixed at DDL time. The PG migration
  (256 -> 384 for Granite) is documented in the roadmap; this provider
  reports its dimension so callers can detect the mismatch.
"""

from __future__ import annotations

import logging
from typing import Any

from severian.infrastructure.vector import HashEmbedding

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "ibm-granite/granite-embedding-107m-multilingual"


class GraniteR2Embedding:
    """Neural embedding via sentence-transformers (Granite R2 family).

    Loads the model lazily; ``encode`` is only available after a
    successful ``load()``. The class always exposes ``dimension`` (the
    model's output dimension) so callers can detect the storage
    migration requirement.
    """

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self.dimension = 384  # Granite Embedding 107M output dim
        self._model: Any = None

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self) -> bool:
        """Load the model. Returns True on success, False on any failure."""
        if self._model is not None:
            return True
        try:
            import os
            from pathlib import Path

            # The model is downloaded once and cached locally. When the cache
            # exists, load OFFLINE: no network request means no
            # "unauthenticated requests to the HF Hub" warning and no
            # download chatter on every session start. First-time installs
            # (no cache) keep online mode so the initial download works.
            model_cache = (
                Path.home()
                / ".cache"
                / "huggingface"
                / "hub"
                / "models--ibm-granite--granite-embedding-107m-multilingual"
            )
            if model_cache.is_dir():
                os.environ.setdefault("HF_HUB_OFFLINE", "1")
            # Silence tqdm progress bars from model loading.
            os.environ.setdefault("TQDM_DISABLE", "1")

            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            return True
        except Exception as exc:  # pragma: no cover - env-dependent
            logger.warning("Granite embedding load failed (%s); falling back to hash", exc)
            return False

    def encode(self, text: str) -> list[float]:
        if self._model is None:
            if not self.load():
                raise RuntimeError("Granite embedding not loaded; call load() or use or_hash()")
        assert self._model is not None
        vector = self._model.encode(
            text, normalize_embeddings=True, show_progress_bar=False
        )
        return [float(value) for value in vector]

    def or_hash(self) -> HashEmbedding:
        """Return a working embedding: neural when loaded, else hash."""
        if self.load():
            return self  # type: ignore[return-value]  # satisfies EmbeddingProvider
        return HashEmbedding(dimension=self.dimension)
