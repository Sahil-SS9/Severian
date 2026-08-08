"""Embedding-space identity contract (zero-debt plan, Task 2.1).

- A fresh index is stamped with the active embedding's metadata.
- Reopening with the same embedding verifies cleanly.
- Reopening a non-empty index with a DIFFERENT space raises
  EmbeddingSpaceMismatchError with rebuild guidance.
- Same dimension ≠ compatible: a 384-dim space from a different model
  must still fail.
- A legacy non-empty index (rows, no metadata) must fail — never
  silently mix spaces.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.infrastructure.embedding_metadata import (  # noqa: E402
    EmbeddingMetadata,
    EmbeddingSpaceMismatchError,
    metadata_from_dict,
)
from severian.infrastructure.embedding_registry import (  # noqa: E402
    resolve_embedding_metadata,
)
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402

PG_DSN = "postgresql://severian:severian@127.0.0.1:54329/severian"


class _FakeNeuralEmbedding:
    """A 384-dim neural-like embedding with a different identity."""

    dimension = 384

    def __init__(self, model_name: str = "fake/model-v1", dimension: int = 384) -> None:
        self.dimension = dimension
        self.model_name = model_name

    def encode(self, text: str) -> list[float]:
        return [1.0] * self.dimension


def _scope() -> dict:
    return {"tenant_id": "t", "profile_id": "p", "collection_id": "c",
            "agent_id": "a", "session_id": "s"}


def _memory(memory_id: str, content: str) -> object:
    from severian.domain.models import Memory, Scope

    return Memory(
        id=memory_id,
        content=content,
        scope=Scope(**_scope()),
        kind="fact",
        source_observation_ids=["obs-1"],
        model_id="test",
        prompt_version="test-v1",
        confidence=1.0,
    )


# --- metadata model ---------------------------------------------------------


def test_fingerprint_is_stable_and_dimension_sensitive() -> None:
    a = EmbeddingMetadata(provider="sentence-transformers", model_id="m/1",
                          revision="abc", dimension=384, normalize=True,
                          pooling="mean", prompt_contract="", runtime="st")
    b = EmbeddingMetadata(provider="sentence-transformers", model_id="m/1",
                          revision="abc", dimension=768, normalize=True,
                          pooling="mean", prompt_contract="", runtime="st")
    c = EmbeddingMetadata(provider="sentence-transformers", model_id="m/1",
                          revision="abc", dimension=384, normalize=True,
                          pooling="mean", prompt_contract="", runtime="st")
    assert a.fingerprint == c.fingerprint
    assert a.fingerprint != b.fingerprint


def test_fingerprint_same_dimension_different_model_differ() -> None:
    """Same dimension must NOT imply compatibility."""
    a = EmbeddingMetadata(provider="sentence-transformers", model_id="m/granite-107m",
                          revision="abc", dimension=384, normalize=True,
                          pooling="mean", prompt_contract="", runtime="st")
    b = EmbeddingMetadata(provider="sentence-transformers", model_id="m/other-384",
                          revision="abc", dimension=384, normalize=True,
                          pooling="mean", prompt_contract="", runtime="st")
    assert a.fingerprint != b.fingerprint


def test_metadata_roundtrip_via_dict() -> None:
    meta = EmbeddingMetadata(provider="hash", model_id="hash", revision="deterministic",
                             dimension=256, normalize=True, pooling="none",
                             prompt_contract="", runtime="pure-python",
                             params={"stem": True})
    restored = metadata_from_dict(meta.to_dict())
    assert restored == meta
    assert restored.fingerprint == meta.fingerprint


def test_registry_resolves_hash_and_fake_neural() -> None:
    hash_meta = resolve_embedding_metadata(HashEmbedding(dimension=256))
    assert hash_meta.provider == "hash"
    assert hash_meta.revision == "deterministic"
    assert hash_meta.dimension == 256

    neural_meta = resolve_embedding_metadata(_FakeNeuralEmbedding())
    assert neural_meta.provider == "sentence-transformers"
    assert neural_meta.model_id == "fake/model-v1"
    assert neural_meta.dimension == 384


def test_registry_conservative_identity_for_unknown_provider() -> None:
    """Unknown providers get a conservative class-keyed identity.

    The EmbeddingProvider protocol is duck-typed, so ANY compliant
    object must remain usable; the fingerprint still changes when the
    provider class or dimension changes (never invents a model_id).
    """
    meta = resolve_embedding_metadata(object())
    assert meta.model_id == "unknown"
    assert meta.provider == "builtins.object"

    class _Other:
        dimension = 64

    other = resolve_embedding_metadata(_Other())
    assert other.provider == "test_embedding_space_identity._Other"
    assert other.dimension == 64
    assert other.fingerprint != meta.fingerprint


# --- sqlite index guard -----------------------------------------------------


def test_fresh_index_stamped_and_reopened_clean(tmp_path: Path) -> None:
    path = tmp_path / "vectors.db"
    idx = LocalVectorIndex(path, HashEmbedding(dimension=256))
    idx.close()
    # Reopen same space → clean.
    idx2 = LocalVectorIndex(path, HashEmbedding(dimension=256))
    idx2.close()


def test_mismatch_on_nonempty_index_raises(tmp_path: Path) -> None:
    path = tmp_path / "vectors.db"
    idx = LocalVectorIndex(path, HashEmbedding(dimension=256))
    idx.upsert(_memory("m1", "alpha fact"))
    idx.close()

    with pytest.raises(EmbeddingSpaceMismatchError) as excinfo:
        LocalVectorIndex(path, HashEmbedding(dimension=384))
    message = str(excinfo.value)
    assert "REBUILD" in message
    assert "rebuild-vectors" in message


def test_same_dimension_different_space_raises(tmp_path: Path) -> None:
    path = tmp_path / "vectors.db"
    idx = LocalVectorIndex(path, HashEmbedding(dimension=384))
    idx.upsert(_memory("m1", "alpha fact"))
    idx.close()

    with pytest.raises(EmbeddingSpaceMismatchError):
        LocalVectorIndex(path, _FakeNeuralEmbedding())


def test_empty_index_can_be_reopened_with_different_space(tmp_path: Path) -> None:
    """An EMPTY index has no data at risk — restamping is safe."""
    path = tmp_path / "vectors.db"
    idx = LocalVectorIndex(path, HashEmbedding(dimension=256))
    idx.close()

    idx2 = LocalVectorIndex(path, _FakeNeuralEmbedding())
    idx2.close()


def test_legacy_nonempty_index_without_meta_raises(tmp_path: Path) -> None:
    """Rows but no vector_space_meta → unknown space → must fail."""
    path = tmp_path / "vectors.db"
    # Build a legacy-shaped index WITHOUT the meta table.
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE memory_vectors (
            memory_id TEXT PRIMARY KEY, tenant_id TEXT, profile_id TEXT,
            collection_id TEXT, agent_id TEXT, session_id TEXT,
            content TEXT, vector TEXT)"""
    )
    conn.execute(
        "INSERT INTO memory_vectors VALUES ('m1','t','p','c','a','s','alpha','[1.0]')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(EmbeddingSpaceMismatchError):
        LocalVectorIndex(path, HashEmbedding(dimension=256))


# --- pgvector guard (scratch container) -------------------------------------


def _pg_available() -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 54329), timeout=1.0):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _pg_available(), reason="PG scratch container not up")
def test_pg_fresh_stamp_and_mismatch() -> None:
    from severian.infrastructure.pgvector_index import PGVectorIndex

    idx = PGVectorIndex(PG_DSN, HashEmbedding(dimension=256))
    idx.reset()  # drop + recreate empty
    idx.initialize()
    idx.close()

    # Same space reopens clean.
    idx2 = PGVectorIndex(PG_DSN, HashEmbedding(dimension=256))
    idx2.initialize()
    idx2.close()

    # Non-empty + different space → mismatch.
    idx3 = PGVectorIndex(PG_DSN, HashEmbedding(dimension=256))
    idx3.initialize()
    idx3.upsert(_memory("pg-m1", "alpha fact"))
    idx3.close()
    with pytest.raises(EmbeddingSpaceMismatchError):
        bad = PGVectorIndex(PG_DSN, _FakeNeuralEmbedding())
        bad.initialize()
    # Cleanup.
    clean = PGVectorIndex(PG_DSN, HashEmbedding(dimension=256))
    clean.reset()
    clean.close()


def test_metadata_json_in_pg(tmp_path: Path) -> None:
    """The metadata dict round-trips through JSON (as persisted)."""
    meta = EmbeddingMetadata(provider="hash", model_id="hash", revision="deterministic",
                             dimension=256, normalize=True, pooling="none",
                             prompt_contract="", runtime="pure-python")
    payload = json.dumps(meta.to_dict(), sort_keys=True)
    restored = metadata_from_dict(json.loads(payload))
    assert restored.fingerprint == meta.fingerprint
