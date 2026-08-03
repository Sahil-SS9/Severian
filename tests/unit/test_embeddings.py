"""Tests for pluggable embeddings (A-1 seam).

Proves the EmbeddingProvider protocol is honored by composition: a
custom provider is accepted, the default remains HashEmbedding, and the
Granite provider degrades gracefully when its optional deps are absent.
"""
from __future__ import annotations

from pathlib import Path

from severian.composition import build_bundle
from severian.infrastructure.granite_embedding import GraniteR2Embedding
from severian.infrastructure.vector import EmbeddingProvider, HashEmbedding


class ConstantEmbedding(EmbeddingProvider):
    """Deterministic non-hash provider used to prove pluggability."""

    dimension: int = 16

    def encode(self, text: str) -> list[float]:
        # Dimension-stable vector that depends on text length (so it is
        # not a hash, and identical inputs map identically).
        return [float(len(text)) / 100.0] * self.dimension


def test_hash_embedding_satisfies_protocol() -> None:
    provider: EmbeddingProvider = HashEmbedding(dimension=256)
    assert provider.dimension == 256
    vector = provider.encode("hello world")
    assert len(vector) == 256
    assert abs(sum(v * v for v in vector) - 1.0) < 1e-6  # unit norm


def test_constant_embedding_is_pluggable(tmp_path: Path) -> None:
    from severian.application.derivation import DerivationService
    from severian.application.worker import DerivationWorker
    from severian.domain.models import Scope

    bundle = build_bundle(
        backend="sqlite",
        database=tmp_path / "db.sqlite",
        fts=tmp_path / "db.fts",
        vectors=tmp_path / "db.vec",
        embedding=ConstantEmbedding(),
    )
    scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    worker = DerivationWorker(
        bundle.repository,
        DerivationService(bundle.repository),
        bundle.projection,
    )
    try:
        # Ingest through the service and confirm search works with the
        # custom embedding (no hash dependency). The worker promotes the
        # observation into a searchable memory.
        bundle.service.ingest_observation(
            scope,
            content="pluggable embedding test content",
            source={"uri": "test://1"},
        )
        worker.drain(worker_id="w-emb", gateway=bundle.gateway)
        hits = bundle.service.search(scope, query="pluggable", limit=5)
        assert len(hits) == 1
    finally:
        bundle.close()


def test_default_embedding_is_hash(tmp_path: Path) -> None:
    bundle = build_bundle(
        backend="sqlite",
        database=tmp_path / "db2.sqlite",
        fts=tmp_path / "db2.fts",
        vectors=tmp_path / "db2.vec",
    )
    try:
        assert isinstance(bundle.vector.embedding, HashEmbedding)
    finally:
        bundle.close()


def test_granite_falls_back_when_deps_missing() -> None:
    # In an environment without sentence-transformers/onnxruntime the
    # provider must not raise on construction and must fall back to a
    # working hash embedding.
    provider = GraniteR2Embedding()
    assert provider.dimension == 384
    fallback = provider.or_hash()
    assert isinstance(fallback, (GraniteR2Embedding, HashEmbedding))
    # If the model could not load, the fallback is HashEmbedding.
    if not provider.loaded:
        assert isinstance(fallback, HashEmbedding)
