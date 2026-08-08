"""Centralised embedding selection (zero-debt plan, Task 2.2).

ONE resolver is called by every production entry point — Hermes
adapter, CLI, composition/default bundle, PG vector index, benchmark
adapter. No caller re-implements ``SEVERIAN_EMBEDDING`` parsing, and
NO caller performs silent fallback substitution.

Selection contract (canonical):
- ``SEVERIAN_EMBEDDING`` unset or ``hash``  → HashEmbedding(dimension=256)
  (the deterministic fallback SPACE — opening an index in this space is
  always explicit).
- ``SEVERIAN_EMBEDDING=granite``           → GraniteR2Embedding() — the
  semantic SPACE. Model load is lazy; a load failure surfaces as a
  visible error at encode time (guarded by the embedding-space
  fingerprint from Task 2.1). The resolver NEVER swaps in hash vectors
  under a semantic identity: that silent substitution is the exact
  incident class this programme exists to prevent.

The returned provider carries a stable identity resolvable via
``embedding_registry.resolve_embedding_metadata``; the index open-time
guard refuses to write a different space into a non-empty index.
"""
from __future__ import annotations

import os
from typing import Any

DEFAULT_HASH_DIMENSION = 256
GRANITE_ENV_VALUE = "granite"
HASH_ENV_VALUE = "hash"


class EmbeddingSelectionError(RuntimeError):
    """The requested embedding space cannot be provided.

    Raised instead of silently substituting a different space.
    """


def parse_embedding_selector(value: str | None) -> str:
    """Canonical parse of the embedding selector (env value or None).

    Returns ``"granite"`` or ``"hash"``. Unknown values are treated as
    ``hash`` — the deterministic default space — and are never treated
    as a semantic selection. (A typo like ``SEVERIAN_EMBEDDING=granit``
    must not silently select hash for a semantic index: the caller
    decides the space; the index fingerprint guard enforces it.)
    """
    raw = (value or "").strip().lower()
    if raw == GRANITE_ENV_VALUE:
        return GRANITE_ENV_VALUE
    return HASH_ENV_VALUE


def resolve_embedding(selector: str | None = None, *, dimension: int | None = None) -> Any:
    """Build the embedding for the given selector (env value or None).

    Never substitutes a space: ``granite`` returns the Granite R2
    provider (lazy load — failures surface at encode, guarded by the
    space fingerprint); ``hash`` returns the deterministic
    :class:`HashEmbedding`. Callers that need a guaranteed-working
    provider for a NEW empty index must select hash EXPLICITLY.
    """
    space = parse_embedding_selector(selector)
    if space == GRANITE_ENV_VALUE:
        from severian.infrastructure.granite_embedding import GraniteR2Embedding

        return GraniteR2Embedding()
    from severian.infrastructure.vector import HashEmbedding

    return HashEmbedding(dimension=dimension or DEFAULT_HASH_DIMENSION)


def embedding_from_env() -> Any:
    """Build the embedding selected by ``SEVERIAN_EMBEDDING``.

    The single entry point every caller uses; the only place the
    environment variable is read.
    """
    return resolve_embedding(os.environ.get("SEVERIAN_EMBEDDING"))


__all__ = [
    "DEFAULT_HASH_DIMENSION",
    "EmbeddingSelectionError",
    "embedding_from_env",
    "parse_embedding_selector",
    "resolve_embedding",
]
