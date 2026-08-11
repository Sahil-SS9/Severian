"""Versioned vector projection storage (zero-debt plan, Task 4.1).

Vectors are stored as packed little-endian float32 BLOBs with explicit
storage-format metadata in vector_space_meta. Legacy JSON-text indexes
are REFUSED at open with rebuild guidance — never mutated in place
(the projection is disposable/rebuildable from canonical memories).

Tests: pack/unpack roundtrip, malformed blob, wrong dimension, NaN/Inf
at write, endian/format mismatch, legacy JSON refusal, canonical source
unchanged after index operations.
"""
from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.domain.errors import VectorFormatError  # noqa: E402
from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.vector import (  # noqa: E402
    VECTOR_STORAGE_FORMAT,
    HashEmbedding,
    LocalVectorIndex,
    _pack_vector,
    _unpack_vector,
)


def _scope() -> Scope:
    return Scope(
        tenant_id="t", profile_id="p", collection_id="c",
        agent_id="a", session_id="s",
    )


def _memory(scope: Scope, i: int = 1) -> Memory:
    return Memory(
        kind="fact",
        content=f"packed vector storage evidence {i}",
        scope=scope,
        source_observation_ids=[f"obs-{i}"],
        model_id="test/hasher",
        prompt_version="test-v1",
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# pack / unpack
# ---------------------------------------------------------------------------


def test_pack_unpack_roundtrip() -> None:
    vector = [0.5, -1.25, 3.75, 0.0, 1.0, -0.5, 2.0, 42.0]
    blob = _pack_vector(vector)
    assert isinstance(blob, bytes)
    assert len(blob) == 8 * 4  # 8 floats × 4 bytes little-endian
    assert _unpack_vector(blob, 8) == pytest.approx(vector)


def test_blob_is_little_endian_float32() -> None:
    # 1.0 in little-endian f32 = 00 00 80 3F; big-endian would be 3F 80 00 00.
    blob = _pack_vector([1.0])
    assert blob == b"\x00\x00\x80\x3f"
    assert struct.unpack("<f", blob)[0] == 1.0


def test_unpack_wrong_dimension_raises() -> None:
    blob = _pack_vector([1.0, 2.0, 3.0, 4.0])
    with pytest.raises(VectorFormatError, match="dimension"):
        _unpack_vector(blob, 8)  # 16 bytes but claims 8-dim (32 bytes)


def test_unpack_malformed_length_raises() -> None:
    blob = b"\x00\x00\x80\x3f" + b"\x01"  # 5 bytes, not a float32 multiple
    with pytest.raises(VectorFormatError):
        _unpack_vector(blob, 1)


def test_unpack_nonfinite_payload_raises() -> None:
    blob = struct.pack("<2f", 1.0, float("nan"))
    with pytest.raises(VectorFormatError, match="non-finite"):
        _unpack_vector(blob, 2)


def test_pack_rejects_nan_and_inf_at_write() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        _pack_vector([1.0, float("nan")])
    with pytest.raises(ValueError, match="non-finite"):
        _pack_vector([1.0, float("inf")])


# ---------------------------------------------------------------------------
# index-level behaviour
# ---------------------------------------------------------------------------


def test_index_stamps_storage_format_on_open(tmp_path: Path) -> None:
    path = tmp_path / "vectors.db"
    index = LocalVectorIndex(path, HashEmbedding(dimension=64))
    try:
        row = index.connection.execute(
            "SELECT value FROM vector_space_meta WHERE key = 'vector_storage_format'"
        ).fetchone()
        assert row is not None
        assert row[0] == VECTOR_STORAGE_FORMAT
    finally:
        index.close()


def test_index_writes_blobs_and_searches(tmp_path: Path) -> None:
    path = tmp_path / "vectors.db"
    index = LocalVectorIndex(path, HashEmbedding(dimension=64))
    try:
        index.upsert(_memory(_scope()))
        stored = index.connection.execute(
            "SELECT vector FROM memory_vectors"
        ).fetchone()[0]
        assert isinstance(stored, bytes)
        assert len(stored) == 64 * 4

        hits = index.search(_scope(), "packed vector storage evidence")
        assert hits and hits[0].memory_id is not None
    finally:
        index.close()


class _OverflowDotEmbedding:
    """Finite f64 query × finite f32 row whose product overflows f64."""

    def __init__(self) -> None:
        self.dimension = 1

    def encode(self, content: str) -> list[float]:
        return [3.4028234663852886e38]

    def encode_query(self, query: str) -> list[float]:
        return [1e308]


def test_search_rejects_nonfinite_dot_product_from_finite_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Python fallback must not rank ``inf`` while native rejects it."""
    monkeypatch.setenv("SEVERIAN_VECTOR_BACKEND", "python")
    index = LocalVectorIndex(tmp_path / "overflow.db", _OverflowDotEmbedding())
    try:
        index.upsert(_memory(_scope()))
        with pytest.raises(VectorFormatError, match="non-finite dot product"):
            index.search(_scope(), "overflow")
    finally:
        index.close()


def test_legacy_json_index_is_refused_with_guidance(tmp_path: Path) -> None:
    """A JSON-format index must be refused, never mutated in place."""
    path = tmp_path / "legacy.db"
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

    with pytest.raises(VectorFormatError, match="rebuild"):
        LocalVectorIndex(path, HashEmbedding(dimension=64))
    # The legacy file is untouched (still raw JSON).
    conn = sqlite3.connect(path)
    row = conn.execute("SELECT vector FROM memory_vectors").fetchone()
    conn.close()
    assert row[0] == "[1.0]"


def test_wrong_storage_format_version_is_refused(tmp_path: Path) -> None:
    """A future/unknown format version must be refused, not guessed at."""
    path = tmp_path / "future.db"
    index = LocalVectorIndex(path, HashEmbedding(dimension=64))
    index.connection.execute(
        "UPDATE vector_space_meta SET value = 'blob_f64be' WHERE key = 'vector_storage_format'"
    )
    index.connection.commit()
    index.close()

    with pytest.raises(VectorFormatError, match="only supports"):
        LocalVectorIndex(path, HashEmbedding(dimension=64))


def test_rebuild_creates_blob_index_from_canonical(tmp_path: Path) -> None:
    """The rebuild path lands blobs; canonical source stays unchanged."""
    from severian.domain.models import Collection
    from severian.infrastructure.sqlite import SQLiteRepository

    repo = SQLiteRepository(tmp_path / "canonical.db")
    repo.initialize()
    repo.create_collection(Collection(id="c", tenant_id="t", name="Primary"))
    scope = _scope()
    from severian.domain.models import Observation

    for i in range(5):
        repo.add_observation(
            Observation(id=f"obs-{i}", scope=scope, content=_memory(scope, i).content)
        )
    repo.add_memories_batch([_memory(scope, i) for i in range(5)])

    path = tmp_path / "vectors.db"
    index = LocalVectorIndex(path, HashEmbedding(dimension=64))
    try:
        index.rebuild(repo, scope)
        stored = index.connection.execute("SELECT vector FROM memory_vectors").fetchone()[0]
        assert isinstance(stored, bytes) and len(stored) == 64 * 4
        # Canonical unchanged: still 5 memories with the same content.
        assert len(repo.list_memories(scope)) == 5
        assert repo.list_memories(scope)[0].content.startswith("packed vector storage")
    finally:
        index.close()
        repo.close()


def test_json_module_no_longer_needed_for_vectors(tmp_path: Path) -> None:
    """Vectors are BLOBs end to end: search must not hit json.loads."""
    path = tmp_path / "vectors.db"
    index = LocalVectorIndex(path, HashEmbedding(dimension=64))
    try:
        index.upsert(_memory(_scope()))
        # If search tried to json.loads a BLOB it would TypeError; this
        # also proves the read path uses _unpack_vector.
        hits = index.search(_scope(), "packed")
        assert isinstance(hits, list)
    finally:
        index.close()
