"""Embedding registry (zero-debt plan, Task 2.1).

Resolves any :class:`EmbeddingProvider` instance to its full
:class:`EmbeddingMetadata` — provider type, model id, revision,
dimension, normalisation, pooling, prompt contract, runtime,
quantisation and the derived fingerprint.

Every production entry point (composition, CLI, Hermes adapter,
nightly/event injection, benchmark adapter) calls this ONE resolver —
no caller re-implements environment parsing or identity derivation.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from severian.infrastructure.embedding_metadata import EmbeddingMetadata

# Model directory → human model id for sentence-transformers lookups.
_ST_MODEL_ID = {
    "ibm-granite--granite-embedding-107m-multilingual": "ibm-granite/granite-embedding-107m-multilingual",
    "ibm-granite--granite-embedding-97m-multilingual-r2": "ibm-granite/granite-embedding-97m-multilingual-r2",
    "ibm-granite--granite-embedding-311m-multilingual-r2": "ibm-granite/granite-embedding-311m-multilingual-r2",
    "ibm-granite--granite-embedding-small-english-r2": "ibm-granite/granite-embedding-small-english-r2",
    "google--embeddinggemma-300m": "google/embeddinggemma-300m",
    "Qwen--Qwen3-Embedding-0.6B": "Qwen/Qwen3-Embedding-0.6B",
    "hotchpotch--bekko-embedding-v1-a8m": "hotchpotch/bekko-embedding-v1-a8m",
}


def _hf_revision(model_id: str) -> str:
    """Best-effort immutable revision: HF snapshot SHA from the cache.

    Falls back to "n/a" when the cache is absent — the fingerprint then
    still pins provider/model/dim/contract, which catches the dangerous
    mismatches (dimension or model change); a missing revision is
    recorded honestly as "n/a", never guessed.
    """
    if model_id == "hash":
        return "deterministic"
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    folder = "models--" + model_id.replace("/", "--")
    snapshot = cache_dir / folder / "snapshots"
    if not snapshot.is_dir():
        return "n/a"
    try:
        commits = sorted(p.name for p in snapshot.iterdir() if p.is_dir())
    except OSError:
        return "n/a"
    return commits[-1] if commits else "n/a"


def resolve_embedding_metadata(embedding: Any) -> EmbeddingMetadata:
    """Resolve any embedding instance to its full identity.

    Recognises:
    - :class:`HashEmbedding` (deterministic fallback space)
    - :class:`GraniteR2Embedding` (legacy sentence-transformers space)
    - :class:`QwenEmbedding` (selected pinned sentence-transformers space)
    - any object exposing ``model_name``/``model_id`` + ``dimension`` +
      ``encode`` (generic sentence-transformers wrapper)
    """
    if embedding is None:
        raise ValueError("cannot resolve metadata for None embedding")

    # Hash embedding: deterministic space.
    module = type(embedding).__module__
    class_name = type(embedding).__name__
    if class_name == "HashEmbedding" or module.endswith("vector") and class_name == "HashEmbedding":
        return EmbeddingMetadata(
            provider="hash",
            model_id="hash",
            revision="deterministic",
            dimension=int(embedding.dimension),
            normalize=True,  # encode() L2-normalises
            pooling="none",
            prompt_contract="",
            runtime="pure-python",
            quantisation="none",
            params={"skip_stopwords": bool(getattr(embedding, "skip_stopwords", True)),
                    "stem": bool(getattr(embedding, "stem", True))},
        )

    # RemoteQwenEmbedding is deliberately the sole named transport variant
    # sharing Qwen's identity. Exact class identity prevents an arbitrary
    # class-name/subclass spoof from inheriting this persisted vector space.
    from severian.infrastructure.qwen_embedding import QwenEmbedding
    from severian.infrastructure.remote_qwen_embedding import RemoteQwenEmbedding

    if type(embedding) in {QwenEmbedding, RemoteQwenEmbedding}:
        model_id = str(getattr(embedding, "model_name", "")).strip() or "Qwen/Qwen3-Embedding-0.6B"
        revision = str(getattr(embedding, "revision", "")).strip()
        if not revision:
            raise ValueError("Qwen embedding is missing its immutable revision")
        prompt_contract = str(getattr(embedding, "prompt_contract", "") or "")
        if not prompt_contract:
            raise ValueError("Qwen embedding is missing its query prompt contract")
        return EmbeddingMetadata(
            provider="sentence-transformers",
            model_id=model_id,
            revision=revision,
            dimension=int(embedding.dimension),
            normalize=True,
            pooling="mean",
            prompt_contract=prompt_contract,
            runtime="sentence-transformers",
            quantisation="none",
            params={"encode_query": "query-only", "truncate_dim": int(embedding.dimension)},
        )

    # Granite R2 embedding (sentence-transformers based).
    if class_name == "GraniteR2Embedding":
        model_id = str(getattr(embedding, "model_name", "")).strip() or (
            "ibm-granite/granite-embedding-107m-multilingual"
        )
        return EmbeddingMetadata(
            provider="sentence-transformers",
            model_id=model_id,
            revision=_hf_revision(model_id),
            dimension=int(embedding.dimension),
            normalize=True,  # encode() uses normalize_embeddings=True
            pooling="mean",
            prompt_contract="",
            runtime="sentence-transformers",
            quantisation="none",
        )

    # Generic sentence-transformers wrapper (bake-off lanes).
    model_id = (str(getattr(embedding, "model_name", ""))
                or str(getattr(embedding, "model_id", ""))).strip()
    if model_id:
        return EmbeddingMetadata(
            provider="sentence-transformers",
            model_id=model_id,
            revision=_hf_revision(model_id),
            dimension=int(embedding.dimension),
            normalize=True,
            pooling="mean",
            prompt_contract=str(getattr(embedding, "prompt", "") or ""),
            runtime="sentence-transformers",
            quantisation="none",
        )

    # Unknown provider — derive a CONSERVATIVE identity, never guess one.
    # The EmbeddingProvider protocol is duck-typed (dimension + encode),
    # so any compliant object must be usable. The conservative identity
    # keys the space on (module, class, dimension, normalize-flag): any
    # provider-class or dimension change alters the fingerprint, which
    # still catches the dangerous mismatches. We do NOT invent a model_id
    # or revision — those stay "unknown" and are recorded honestly.
    return EmbeddingMetadata(
        provider=f"{module}.{class_name}",
        model_id="unknown",
        revision="unknown",
        dimension=int(getattr(embedding, "dimension", 0)),
        normalize=bool(getattr(embedding, "normalize", True)),
        pooling="unknown",
        prompt_contract="",
        runtime="unknown",
        quantisation="none",
    )


def fingerprint_of(embedding: Any) -> str:
    return resolve_embedding_metadata(embedding).fingerprint


def metadata_to_json(metadata: EmbeddingMetadata) -> str:
    return json.dumps(metadata.to_dict(), sort_keys=True)


__all__ = [
    "fingerprint_of",
    "metadata_to_json",
    "resolve_embedding_metadata",
]
