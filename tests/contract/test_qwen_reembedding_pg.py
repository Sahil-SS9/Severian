"""Live disposable-PostgreSQL proof for Qwen's same-dimension migration.

The test intentionally requires both ``SEVERIAN_PG_DSN`` and the explicit
``SEVERIAN_PG_ALLOW_RESET=1`` acknowledgement.  It must never infer or use a
production DSN.
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.embedding_metadata import EmbeddingSpaceMismatchError  # noqa: E402
from severian.infrastructure.pgvector_index import PGVectorIndex  # noqa: E402
from severian.infrastructure.qwen_embedding import (  # noqa: E402
    QWEN_DIMENSION,
    QWEN_RETRIEVAL_PROMPT,
    QwenEmbedding,
)
from severian.infrastructure.rebuild import rebuild_pg_vectors  # noqa: E402

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")
RESET_ACKNOWLEDGED = os.environ.get("SEVERIAN_PG_ALLOW_RESET") == "1"


class _Granite384:
    dimension = QWEN_DIMENSION
    model_name = "ibm-granite/granite-embedding-107m-multilingual"

    def encode(self, text: str) -> list[float]:
        return _vector(text)


class _RecordingQwenModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def encode(self, text: str, **kwargs: object) -> list[float]:
        self.calls.append({"text": text, **kwargs})
        return _vector(text)


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
    return Scope(
        tenant_id="qwen-t",
        profile_id="qwen-p",
        collection_id="qwen-c",
        agent_id="qwen-a",
        session_id="qwen-s",
    )


def _memories(scope: Scope) -> list[Memory]:
    return [
        Memory(
            id=f"qwen-pg-{index:02d}",
            content=f"PostgreSQL Qwen migration document {index} with distinct content.",
            scope=scope,
            kind="fact",
            source_observation_ids=[f"obs-{index:02d}"],
            model_id="test",
            prompt_version="test-v1",
            confidence=1.0,
        )
        for index in range(12)
    ]


def _drop_scratch_tables() -> None:
    from sqlalchemy import create_engine

    dsn = PG_DSN.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_engine(dsn)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('DROP TABLE IF EXISTS "memory_vectors" CASCADE')
            connection.exec_driver_sql('DROP TABLE IF EXISTS "memory_vectors_meta" CASCADE')
    finally:
        engine.dispose()


@pytest.mark.skipif(not PG_DSN, reason="SEVERIAN_PG_DSN not set; requires disposable pgvector test DSN")
@pytest.mark.skipif(not RESET_ACKNOWLEDGED, reason="SEVERIAN_PG_ALLOW_RESET=1 required for disposable PostgreSQL proof")
def test_pg_qwen_migration_rejects_granite_then_swaps_with_query_prompt() -> None:
    scope = _scope()
    repository = _Repository(_memories(scope))
    _drop_scratch_tables()
    model = _RecordingQwenModel()
    qwen = QwenEmbedding()
    qwen._model = model
    try:
        granite = PGVectorIndex(PG_DSN, _Granite384())
        granite.initialize()
        granite.upsert_many(repository.list_memories(scope))
        granite.close()

        rejected = PGVectorIndex(PG_DSN, qwen)
        with pytest.raises(EmbeddingSpaceMismatchError, match="rebuild-vectors"):
            rejected.initialize()
        rejected.close()

        result = rebuild_pg_vectors(PG_DSN, repository, scope, qwen)

        assert result.status == "swapped", result.error
        assert result.verification is not None and result.verification.passed
        assert result.rollback_path is not None
        reopened = PGVectorIndex(PG_DSN, qwen)
        assert reopened.count(scope) == len(repository.list_memories(scope))
        reopened.close()
        assert any(call.get("prompt") == QWEN_RETRIEVAL_PROMPT for call in model.calls)
        assert any("prompt" not in call for call in model.calls)
    finally:
        _drop_scratch_tables()
