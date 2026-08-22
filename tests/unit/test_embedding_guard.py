"""Fail-closed guard tests for embedding_from_env(require_explicit=True)."""
from __future__ import annotations

import pytest

from severian.infrastructure.embedding_resolver import (
    EmbeddingSelectionError,
    embedding_from_env,
)


def test_require_explicit_refuses_unset_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_EMBEDDING", raising=False)
    with pytest.raises(EmbeddingSelectionError, match="is unset"):
        embedding_from_env(require_explicit=True)


def test_require_explicit_refuses_blank_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_EMBEDDING", "   ")
    with pytest.raises(EmbeddingSelectionError):
        embedding_from_env(require_explicit=True)


def test_require_explicit_honours_explicit_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    """The incident was the SILENT default; an explicit hash declaration
    remains legitimate (hash-space stores exist and are tested)."""
    monkeypatch.setenv("SEVERIAN_EMBEDDING", "hash")
    from severian.infrastructure.vector import HashEmbedding

    provider = embedding_from_env(require_explicit=True)
    assert isinstance(provider, HashEmbedding)


def test_without_flag_unset_still_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Normal runtime paths keep the documented default behaviour."""
    monkeypatch.delenv("SEVERIAN_EMBEDDING", raising=False)
    provider = embedding_from_env()
    assert provider is not None


def test_require_explicit_allows_remote_qwen(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("SEVERIAN_EMBEDDING", "remote-qwen")
    monkeypatch.setenv("SEVERIAN_EMBED_SOCKET", str(tmp_path / "embed.sock"))
    from severian.infrastructure.remote_qwen_embedding import RemoteQwenEmbedding

    provider = embedding_from_env(require_explicit=True)
    assert isinstance(provider, RemoteQwenEmbedding)
