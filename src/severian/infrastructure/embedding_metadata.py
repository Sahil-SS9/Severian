"""Embedding-space identity (zero-debt plan, Task 2.1).

One stable identity for every embedding space. Persisted next to the
vector projection (sqlite meta table / PG meta table) and validated on
every open: a non-empty index whose stored identity differs from the
embedding about to write into it must FAIL with
:class:`EmbeddingSpaceMismatchError` and exact rebuild guidance.

Same dimension does NOT imply compatibility: two 384-dim spaces from
different models (or different normalisation/prompt contracts) are
different spaces. The fingerprint captures every property that changes
the vectors.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

FINGERPRINT_VERSION = 1


class EmbeddingSpaceMismatchError(RuntimeError):
    """The index's stored embedding space differs from the active one.

    Carries exact rebuild guidance: never silently rewrite an index
    with vectors from another space (the 2026-08-08 silent 256→384
    incident class).
    """

    def __init__(self, stored: EmbeddingMetadata, active: EmbeddingMetadata,
                 path_or_dsn: str) -> None:
        self.stored = stored
        self.active = active
        self.path_or_dsn = path_or_dsn
        super().__init__(
            f"embedding-space mismatch at {path_or_dsn}:\n"
            f"  stored: {stored.fingerprint} ({stored.provider} / {stored.model_id} "
            f"dim={stored.dimension} rev={stored.revision})\n"
            f"  active: {active.fingerprint} ({active.provider} / {active.model_id} "
            f"dim={active.dimension} rev={active.revision})\n"
            f"REBUILD: run `severian rebuild-vectors` against canonical memories "
            f"to produce a matching index; never open this index for writes "
            f"with a different embedding space."
        )


@dataclass(frozen=True)
class EmbeddingMetadata:
    """The full identity of one embedding space."""

    # Provider/adapter type, e.g. "hash", "sentence-transformers", "onnx".
    provider: str
    # Model ID (HuggingFace id or "hash").
    model_id: str
    # Immutable revision: HF commit SHA for neural models; "deterministic"
    # for hash; "n/a" when unknowable.
    revision: str
    # Output dimension (vector length).
    dimension: int
    # Whether vectors are L2-normalised by the provider.
    normalize: bool
    # Pooling (e.g. "mean", "none" for hash).
    pooling: str
    # Query/document instruction or prefix contract (empty = none).
    prompt_contract: str
    # Runtime ("pure-python", "sentence-transformers", "onnxruntime", ...).
    runtime: str
    # Quantisation ("none", "int8", ...).
    quantisation: str = "none"
    # Additional parameters that change the space (e.g. hash tokenizer).
    params: dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.model_id.strip():
            raise ValueError("model_id must not be empty")
        if self.dimension < 0:
            raise ValueError("dimension must be non-negative (0 = unknown, used only by the mismatch sentinel)")
        if self.params is None:
            object.__setattr__(self, "params", {})

    @property
    def fingerprint(self) -> str:
        """Stable space fingerprint: sha256 over canonical JSON.

        Versioned so a deliberate contract change (new identity fields)
        produces a fresh fingerprint rather than silently matching old
        identities.
        """
        payload = {
            "version": FINGERPRINT_VERSION,
            "provider": self.provider,
            "model_id": self.model_id,
            "revision": self.revision,
            "dimension": self.dimension,
            "normalize": self.normalize,
            "pooling": self.pooling,
            "prompt_contract": self.prompt_contract,
            "runtime": self.runtime,
            "quantisation": self.quantisation,
            "params": dict(sorted(self.params.items())),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "fingerprint": self.fingerprint}


def metadata_from_dict(data: dict[str, Any]) -> EmbeddingMetadata:
    """Rebuild metadata from a persisted dict (fingerprint recomputed)."""
    keys = {"provider", "model_id", "revision", "dimension", "normalize",
            "pooling", "prompt_contract", "runtime", "quantisation"}
    missing = keys - set(data)
    if missing:
        raise ValueError(f"stored embedding metadata missing fields: {sorted(missing)}")
    return EmbeddingMetadata(
        provider=data["provider"],
        model_id=data["model_id"],
        revision=data["revision"],
        dimension=int(data["dimension"]),
        normalize=bool(data["normalize"]),
        pooling=data["pooling"],
        prompt_contract=data["prompt_contract"],
        runtime=data["runtime"],
        quantisation=data.get("quantisation", "none"),
        params=dict(data.get("params", {}) or {}),
    )


__all__ = [
    "EmbeddingMetadata",
    "EmbeddingSpaceMismatchError",
    "FINGERPRINT_VERSION",
    "metadata_from_dict",
]
