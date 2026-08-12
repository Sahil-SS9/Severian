"""Centralised embedding selection (zero-debt plan, Task 2.2).

ONE resolver is called by every production entry point — Hermes
adapter, CLI, composition/default bundle, PG vector index, benchmark
adapter. No caller re-implements ``SEVERIAN_EMBEDDING`` parsing, and
NO caller performs silent fallback substitution.

Selection contract (canonical):
- ``SEVERIAN_EMBEDDING`` unset or ``hash`` → HashEmbedding(dimension=256)
  (the deterministic fallback SPACE — opening an index in this space is
  always explicit).
- ``SEVERIAN_EMBEDDING=qwen`` → QwenEmbedding() — the selected semantic
  SPACE: pinned Qwen3-Embedding-0.6B at 384 dimensions with its
  query-only retrieval instruction.
- ``SEVERIAN_EMBEDDING=remote-qwen`` → RemoteQwenEmbedding() — the same
  selected Qwen SPACE served only through the required private Unix socket
  named by ``SEVERIAN_EMBED_SOCKET``. It never falls back in-process.
- ``SEVERIAN_EMBEDDING=granite`` → GraniteR2Embedding() — an explicit
  legacy semantic SPACE, retained for migration and rollback only.

Semantic model load is lazy; a load failure surfaces as a visible error
at encode time (guarded by the embedding-space fingerprint). The resolver
NEVER swaps in hash vectors under a semantic identity: that silent
substitution is the exact incident class this programme exists to prevent.

The returned provider carries a stable identity resolvable via
``embedding_registry.resolve_embedding_metadata``; the index open-time
guard refuses to write a different space into a non-empty index.
"""
from __future__ import annotations

import os
from typing import Any

DEFAULT_HASH_DIMENSION = 256
QWEN_ENV_VALUE = "qwen"
REMOTE_QWEN_ENV_VALUE = "remote-qwen"
GRANITE_ENV_VALUE = "granite"
HASH_ENV_VALUE = "hash"
REMOTE_QWEN_SOCKET_ENV = "SEVERIAN_EMBED_SOCKET"


class EmbeddingSelectionError(RuntimeError):
    """The requested embedding space cannot be provided.

    Raised instead of silently substituting a different space.
    """


def parse_embedding_selector(value: str | None) -> str:
    """Canonical parse of the embedding selector (env value or None).

    Returns ``"qwen"``, ``"remote-qwen"``, ``"granite"`` or ``"hash"``.
    Unset, blank, and explicit ``"hash"`` select the deterministic default;
    any other non-empty value raises :class:`EmbeddingSelectionError`. A typo
    such as ``SEVERIAN_EMBEDDING=qwen3`` must never create a fresh hash vector
    space.
    """
    raw = (value or "").strip().lower()
    if not raw or raw == HASH_ENV_VALUE:
        return HASH_ENV_VALUE
    if raw == QWEN_ENV_VALUE:
        return QWEN_ENV_VALUE
    if raw == REMOTE_QWEN_ENV_VALUE:
        return REMOTE_QWEN_ENV_VALUE
    if raw == GRANITE_ENV_VALUE:
        return GRANITE_ENV_VALUE
    raise EmbeddingSelectionError(f"unsupported embedding selector: {raw!r}")


def resolve_embedding(selector: str | None = None, *, dimension: int | None = None) -> Any:
    """Build the embedding for the given selector (env value or None).

    Never substitutes a space: ``qwen`` returns the selected pinned Qwen
    provider, ``granite`` returns the explicit legacy Granite provider,
    and ``hash`` returns the deterministic :class:`HashEmbedding`.
    Semantic load failures surface at encode time; callers that need a
    guaranteed-working provider for a NEW empty index must select hash
    EXPLICITLY.
    """
    space = parse_embedding_selector(selector)
    if space == QWEN_ENV_VALUE:
        from severian.infrastructure.qwen_embedding import QwenEmbedding

        return QwenEmbedding()
    if space == REMOTE_QWEN_ENV_VALUE:
        socket_path = os.environ.get(REMOTE_QWEN_SOCKET_ENV, "").strip()
        if not socket_path:
            raise EmbeddingSelectionError(
                f"{REMOTE_QWEN_SOCKET_ENV} is required when SEVERIAN_EMBEDDING=remote-qwen"
            )
        if not os.path.isabs(socket_path):
            raise EmbeddingSelectionError(f"{REMOTE_QWEN_SOCKET_ENV} must be an absolute Unix socket path")
        from severian.infrastructure.remote_qwen_embedding import RemoteQwenEmbedding

        return RemoteQwenEmbedding(socket_path)
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
    "GRANITE_ENV_VALUE",
    "HASH_ENV_VALUE",
    "QWEN_ENV_VALUE",
    "REMOTE_QWEN_ENV_VALUE",
    "REMOTE_QWEN_SOCKET_ENV",
    "embedding_from_env",
    "parse_embedding_selector",
    "resolve_embedding",
]
