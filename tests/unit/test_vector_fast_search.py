"""0.6.4 E: vectorised SQLite vector search, on by default, byte-identical to the pure-Python oracle.

0.6.3 unpacked every stored vector in Python on every search (~1.65 s at 31k vectors). The NumPy
path existed but was opt-in (``SEVERIAN_OPT_NUMPY=1``) and NumPy was not a dependency, so the
gateways never had it. 0.6.4 ships NumPy as a runtime dependency and uses the matrix path by
default. Scores must stay the exact ``math.fsum`` values and ties must order exactly as before.
"""

from __future__ import annotations

import math
import random
import tomllib
from pathlib import Path

import pytest

import severian.infrastructure.vector as vector_module
from severian.domain.models import Memory, Scope
from severian.infrastructure.vector import LocalVectorIndex

ROOT = Path(__file__).resolve().parents[2]
SCOPE = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
DIM = 16


class TableEmbedding:
    """Vectors chosen by the test: text -> vector (queries use the same table)."""

    dimension = DIM

    def __init__(self, table: dict[str, list[float]]) -> None:
        self.table = table

    def encode(self, text: str) -> list[float]:
        return self.table[text]


def _memory(content: str, memory_id: str) -> Memory:
    return Memory(
        id=memory_id,
        scope=SCOPE,
        kind="fact",
        content=content,
        source_observation_ids=["o1"],
        model_id="m",
        prompt_version="v",
        confidence=1.0,
    )


def _index(tmp_path: Path, rows: int, seed: int) -> tuple[LocalVectorIndex, dict[str, list[float]]]:
    rng = random.Random(seed)
    table: dict[str, list[float]] = {}
    # A few distinct vectors repeated many times (exact score ties), plus near-ties that differ in
    # the last bits, plus random rows.
    shared = [[rng.uniform(-1, 1) for _ in range(DIM)] for _ in range(4)]
    memories = []
    for i in range(rows):
        if i % 5 == 0:
            vec = list(shared[i % 4])
        elif i % 7 == 0:
            vec = [v + (1e-7 if j == 0 else 0.0) for j, v in enumerate(shared[(i // 7) % 4])]
        else:
            vec = [rng.uniform(-1, 1) for _ in range(DIM)]
        content = f"row {i % 37}"  # duplicate contents too, so the memory_id tiebreak matters
        table.setdefault(content, vec)
        memories.append((content, f"id-{rng.randrange(10**9):09d}-{i}", vec))
    embedding = TableEmbedding({})
    index = LocalVectorIndex(tmp_path / f"v{seed}.db", embedding)  # type: ignore[arg-type]
    for content, memory_id, vec in memories:
        embedding.table[content] = vec
        index.upsert(_memory(content, memory_id))
    for q in range(6):
        table[f"query {q}"] = list(shared[q % 4]) if q < 4 else [rng.uniform(-1, 1) for _ in range(DIM)]
    embedding.table.update(table)
    return index, table


def _reference(index: LocalVectorIndex, query: str, limit: int) -> list[tuple[str, float]]:
    """The 0.6.3 pure-Python contract: fsum score, order (score desc, content, memory_id)."""
    q = index.embedding.encode(query)
    rows = index.connection.execute(
        "SELECT memory_id, vector, content FROM memory_vectors"
    ).fetchall()
    scored = []
    for memory_id, blob, content in rows:
        vec = vector_module._unpack_vector(blob, DIM)
        scored.append((-math.fsum(a * b for a, b in zip(q, vec, strict=True)), content, memory_id))
    scored.sort()
    return [(memory_id, -neg) for neg, _content, memory_id in scored[:limit]]


def test_numpy_is_a_runtime_dependency() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert any(dep.split(">")[0].split("=")[0].strip() == "numpy" for dep in project["dependencies"])


def test_fast_search_is_the_default_without_any_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_OPT_NUMPY", raising=False)
    assert vector_module.vector_search_backend() == "numpy"


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_fast_search_is_byte_identical_to_the_python_oracle(tmp_path: Path, seed: int) -> None:
    index, table = _index(tmp_path, rows=600, seed=seed)
    try:
        for query in [q for q in table if q.startswith("query")]:
            for limit in (1, 3, 10, 20, 50, 1000):
                expected = _reference(index, query, limit)
                got = [(hit.memory_id, hit.score) for hit in index.search(SCOPE, query, limit=limit)]
                assert got == expected, (query, limit)
    finally:
        index.close()


def test_python_fallback_matches_the_fast_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    index, table = _index(tmp_path, rows=300, seed=9)
    try:
        fast = {q: [(h.memory_id, h.score) for h in index.search(SCOPE, q, limit=25)] for q in table if "query" in q}
        monkeypatch.setattr(vector_module, "_NUMPY_AVAILABLE", False)
        slow = {q: [(h.memory_id, h.score) for h in index.search(SCOPE, q, limit=25)] for q in table if "query" in q}
        assert fast == slow
    finally:
        index.close()


def test_fast_search_still_rejects_a_corrupt_vector(tmp_path: Path) -> None:
    from severian.domain.errors import VectorFormatError

    index, table = _index(tmp_path, rows=20, seed=4)
    try:
        index.connection.execute("UPDATE memory_vectors SET vector = ? WHERE rowid = 3", (b"\x00\x01",))
        index.connection.commit()
        with pytest.raises(VectorFormatError):
            index.search(SCOPE, "query 0", limit=5)
    finally:
        index.close()


def test_near_ties_below_float32_resolution_order_exactly_as_the_oracle(tmp_path: Path) -> None:
    """Rows that differ by far less than float32 can resolve: the fast path must still rank them by
    the exact fsum score (then content, then id), because it rescores every boundary candidate."""
    rng = random.Random(11)
    base = [rng.uniform(-2, 2) for _ in range(DIM)]
    embedding = TableEmbedding({})
    index = LocalVectorIndex(tmp_path / "ties.db", embedding)  # type: ignore[arg-type]
    try:
        for i in range(400):
            vec = list(base)
            vec[rng.randrange(DIM)] += rng.choice([0.0, 1e-9, -1e-9, 3e-8, -3e-8, 1e-6])
            content = f"tie {i % 9}"
            embedding.table[content] = vec
            index.upsert(_memory(content, f"t-{rng.randrange(10**6):06d}-{i}"))
        embedding.table["q"] = [rng.uniform(-3, 3) for _ in range(DIM)]
        for limit in (1, 2, 5, 17, 50, 400):
            expected = _reference(index, "q", limit)
            assert [(h.memory_id, h.score) for h in index.search(SCOPE, "q", limit=limit)] == expected, limit
    finally:
        index.close()
