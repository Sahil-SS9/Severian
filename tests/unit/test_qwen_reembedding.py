"""Qwen re-embedding migration safety over the real SQLite index seam.

Both source and target vectors are 384 dimensions.  These tests prove that the
identity fingerprint, not dimension, prevents Granite/Qwen mixing; the existing
side-by-side rebuild is then used as the deterministic, restartable migration.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.embedding_metadata import EmbeddingSpaceMismatchError  # noqa: E402
from severian.infrastructure.qwen_embedding import QWEN_DIMENSION, QwenEmbedding  # noqa: E402
from severian.infrastructure.rebuild import rebuild_sqlite_vectors  # noqa: E402
from severian.infrastructure.vector import LocalVectorIndex  # noqa: E402


class _Granite384:
    """Legacy Granite-shaped source identity with deterministic 384 vectors."""

    dimension = QWEN_DIMENSION
    model_name = "ibm-granite/granite-embedding-107m-multilingual"

    def encode(self, text: str) -> list[float]:
        return _vector(text)


class _QwenModel:
    def encode(self, text: str, **_kwargs: object) -> list[float]:
        return _vector(text)


class _FailingQwenModel:
    def encode(self, _text: str, **_kwargs: object) -> list[float]:
        raise RuntimeError("interrupted Qwen encode")


class _Repository:
    def __init__(self, memories: list[Memory]) -> None:
        self._memories = memories

    def list_memories(self, scope: Scope) -> list[Memory]:
        return [memory for memory in self._memories if memory.scope == scope]


def _vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    values = [0.0] * QWEN_DIMENSION
    for index, byte in enumerate(digest):
        values[(index * 37 + byte) % QWEN_DIMENSION] = float(index + 1)
    return values


def _scope() -> Scope:
    return Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")


def _memories(scope: Scope, count: int = 12) -> list[Memory]:
    return [
        Memory(
            id=f"memory-{index:02d}",
            content=f"Qwen migration document {index} has its own distinct payload.",
            scope=scope,
            kind="fact",
            source_observation_ids=[f"observation-{index:02d}"],
            model_id="test",
            prompt_version="test-v1",
            confidence=1.0,
        )
        for index in range(count)
    ]


def _qwen(model: object | None = None) -> QwenEmbedding:
    provider = QwenEmbedding()
    provider._model = model or _QwenModel()
    return provider


def _seed_granite(path: Path, repository: _Repository, scope: Scope) -> None:
    index = LocalVectorIndex(path, _Granite384())
    index.upsert_many(repository.list_memories(scope))
    index.close()


def test_sqlite_qwen_migration_rejects_mixed_granite_then_swaps(tmp_path: Path) -> None:
    scope = _scope()
    repository = _Repository(_memories(scope))
    live = tmp_path / "vectors.db"
    _seed_granite(live, repository, scope)

    with pytest.raises(EmbeddingSpaceMismatchError, match="rebuild-vectors"):
        LocalVectorIndex(live, _qwen())

    result = rebuild_sqlite_vectors(live, repository, scope, _qwen())

    assert result.status == "swapped", result.error
    assert result.verification is not None and result.verification.passed
    assert result.rollback_path is not None
    assert Path(result.rollback_path).exists()
    qwen_index = LocalVectorIndex(live, _qwen())
    assert qwen_index.count(scope) == len(repository.list_memories(scope))
    qwen_index.close()
    with pytest.raises(EmbeddingSpaceMismatchError):
        LocalVectorIndex(live, _Granite384())


def test_sqlite_qwen_reembedding_restarts_after_interruption_without_mixing(tmp_path: Path) -> None:
    scope = _scope()
    repository = _Repository(_memories(scope))
    live = tmp_path / "vectors.db"
    _seed_granite(live, repository, scope)
    before = live.read_bytes()

    failed = rebuild_sqlite_vectors(live, repository, scope, _qwen(_FailingQwenModel()))

    assert failed.status == "failed"
    assert live.read_bytes() == before
    granite = LocalVectorIndex(live, _Granite384())
    assert granite.count(scope) == len(repository.list_memories(scope))
    granite.close()
    assert not list(tmp_path.glob("*.rebuild-*"))

    restarted = rebuild_sqlite_vectors(live, repository, scope, _qwen())

    assert restarted.status == "swapped", restarted.error
    qwen = LocalVectorIndex(live, _qwen())
    assert qwen.count(scope) == len(repository.list_memories(scope))
    qwen.close()
