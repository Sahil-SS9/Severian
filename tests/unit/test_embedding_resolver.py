"""Central resolver contract (zero-debt plan, Task 2.2).

ONE resolver owns embedding selection. No caller re-implements
SEVERIAN_EMBEDDING parsing; no caller performs silent hash
substitution for a semantic selection. Load failure must surface
visibly (guarded by the Task 2.1 fingerprint), never as a space swap.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.infrastructure.embedding_resolver import (  # noqa: E402
    parse_embedding_selector,
    resolve_embedding,
)
from severian.infrastructure.vector import HashEmbedding  # noqa: E402


def test_parse_canonical_selector() -> None:
    assert parse_embedding_selector(None) == "hash"
    assert parse_embedding_selector("") == "hash"
    assert parse_embedding_selector("QWEN") == "qwen"
    assert parse_embedding_selector("  qwen  ") == "qwen"
    assert parse_embedding_selector("GRANITE") == "granite"
    assert parse_embedding_selector("  granite  ") == "granite"
    assert parse_embedding_selector("hash") == "hash"
    # Unknown values never select a semantic space.
    assert parse_embedding_selector("qwen3") == "hash"
    assert parse_embedding_selector("granit") == "hash"
    assert parse_embedding_selector("gpt") == "hash"


def test_hash_selector_returns_hash_embedding() -> None:
    emb = resolve_embedding("hash")
    assert isinstance(emb, HashEmbedding)
    assert emb.dimension == 256


def test_unset_selector_returns_default_hash() -> None:
    emb = resolve_embedding(None)
    assert isinstance(emb, HashEmbedding)
    assert emb.dimension == 256


def test_qwen_selector_returns_qwen_not_hash() -> None:
    """The default semantic selector resolves to the named Qwen provider."""
    emb = resolve_embedding("qwen")
    assert not isinstance(emb, HashEmbedding)
    assert type(emb).__name__ == "QwenEmbedding"
    assert emb.dimension == 384


def test_granite_selector_remains_explicit_legacy_granite() -> None:
    """Legacy selection stays Granite; it must never silently alias Qwen."""
    emb = resolve_embedding("granite")
    assert not isinstance(emb, HashEmbedding)
    assert type(emb).__name__ == "GraniteR2Embedding"
    assert emb.dimension == 384


def test_no_silent_substitution_under_qwen_semantic_identity() -> None:
    """A Qwen selection stays Qwen even when the model is missing."""
    import severian.infrastructure.embedding_registry as registry

    emb = resolve_embedding("qwen")
    meta = registry.resolve_embedding_metadata(emb)
    assert meta.provider == "sentence-transformers"
    assert meta.dimension == 384
    assert meta.model_id == "Qwen/Qwen3-Embedding-0.6B"


def test_no_silent_substitution_under_legacy_granite_identity() -> None:
    """A Granite selection stays Granite even when the model is missing.

    The resolver must not catch load failure and swap in hash — the
    incident class this programme exists to prevent. The provider is
    lazy; a missing model surfaces at encode time (or via the index
    fingerprint guard), never as a space change here.
    """
    import severian.infrastructure.embedding_registry as registry

    emb = resolve_embedding("granite")
    meta = registry.resolve_embedding_metadata(emb)
    assert meta.provider == "sentence-transformers"
    assert meta.dimension == 384
    assert meta.model_id != "hash"


def test_env_entry_point_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from severian.infrastructure.embedding_resolver import embedding_from_env

    monkeypatch.setenv("SEVERIAN_EMBEDDING", "qwen")
    assert type(embedding_from_env()).__name__ == "QwenEmbedding"

    monkeypatch.setenv("SEVERIAN_EMBEDDING", "granite")
    assert type(embedding_from_env()).__name__ == "GraniteR2Embedding"

    monkeypatch.setenv("SEVERIAN_EMBEDDING", "hash")
    assert isinstance(embedding_from_env(), HashEmbedding)

    monkeypatch.delenv("SEVERIAN_EMBEDDING")
    assert isinstance(embedding_from_env(), HashEmbedding)


def test_hermes_adapter_delegates_no_local_parsing() -> None:
    """The Hermes adapter calls the resolver — no env parsing remains."""
    import inspect

    import severian.integrations.hermes as hermes

    source = inspect.getsource(hermes._resolve_embedding)
    assert "embedding_resolver" in source
    assert "os.environ" not in source
    assert "or_hash" not in source


def test_cli_delegates_no_local_parsing() -> None:
    import inspect

    import severian.cli.app as cli

    source = inspect.getsource(cli._embedding_from_env)
    assert "embedding_resolver" in source
    assert "os.environ" not in source
