"""Exact bounded top-k (zero-debt plan, Task 4.2).

- heap-bounded selection returns EXACTLY the same rows and order as the
  previous full sort (score desc, content, memory_id) — semantics are
  unchanged by construction;
- the composite scope index exists so scope-filtered queries are not
  full scans;
- dimension validation is retained end to end (read path);
- behaviour at the boundaries: k < N, k == N, k > N.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


def _scope() -> Scope:
    return Scope(
        tenant_id="t", profile_id="p", collection_id="c",
        agent_id="a", session_id="s",
    )


def _memory(scope: Scope, i: int, content: str) -> Memory:
    return Memory(
        kind="fact",
        content=content,
        scope=scope,
        source_observation_ids=[f"obs-{i}"],
        model_id="test/hasher",
        prompt_version="test-v1",
        confidence=0.9,
    )


def _full_sort_reference(index: LocalVectorIndex, scope: Scope, query: str, limit: int) -> list[str]:
    """The pre-4.2 reference: fetch all, sort by (score desc, content, id)."""
    query_vector = index.embedding.encode(query)
    rows = index.connection.execute(
        "SELECT memory_id, vector, content FROM memory_vectors "
        "WHERE tenant_id=? AND profile_id=? AND collection_id=? AND agent_id=? AND session_id=?",
        tuple(scope.model_dump().values()),
    ).fetchall()
    from severian.infrastructure.vector import _unpack_vector

    scored = []
    for memory_id, encoded, content in rows:
        vector = _unpack_vector(encoded, index.embedding.dimension)
        score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
        scored.append((memory_id, score, content))
    scored.sort(key=lambda item: (-item[1], item[2], item[0]))
    return [item[0] for item in scored[:limit]]


@pytest.fixture()
def index(tmp_path: Path):
    idx = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    scope = _scope()
    # Deterministic corpus with intentional near-duplicate contents so
    # hash vectors collide → score ties are exercised heavily.
    corpus = [
        ("alpha backup job runs at 02:00 utc", i)
        for i in range(40)
    ] + [
        ("beta backup job runs at 02:00 utc", i)
        for i in range(40)
    ] + [
        ("gamma report is generated nightly", i)
        for i in range(40)
    ]
    for j, (content, _i) in enumerate(corpus):
        idx.upsert(_memory(scope, j, content))
    yield idx, scope
    idx.close()


def test_bounded_topk_matches_full_sort_order(index) -> None:
    idx, scope = index
    for query in ("backup job", "nightly report", "alpha", "gamma"):
        for limit in (1, 5, 20, 120):
            hits = idx.search(scope, query, limit=limit)
            bounded_ids = [h.memory_id for h in hits]
            reference_ids = _full_sort_reference(idx, scope, query, limit)
            assert bounded_ids == reference_ids, (
                f"query={query!r} limit={limit}: bounded {bounded_ids[:8]}... "
                f"!= full-sort {reference_ids[:8]}..."
            )
            assert len(bounded_ids) == min(limit, 120)


def test_tiebreak_order_is_score_then_content_then_id(index) -> None:
    idx, scope = index
    hits = idx.search(scope, "02:00 utc", limit=120)
    assert len(hits) == 120  # heavy score ties across clusters
    # Real check: every adjacent pair is ordered by the total key.
    query_vector = idx.embedding.encode("02:00 utc")
    scored = []
    for memory_id, encoded, content in idx.connection.execute(
        "SELECT memory_id, vector, content FROM memory_vectors "
        "WHERE tenant_id=? AND profile_id=? AND collection_id=? AND agent_id=? AND session_id=?",
        tuple(scope.model_dump().values()),
    ):
        from severian.infrastructure.vector import _unpack_vector

        vector = _unpack_vector(encoded, idx.embedding.dimension)
        score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
        scored.append((memory_id, score, content))
    for left, right in zip(hits, hits[1:], strict=False):
        left_data = next(item for item in scored if item[0] == left.memory_id)
        right_data = next(item for item in scored if item[0] == right.memory_id)
        assert (-left_data[1], left_data[2], left_data[0]) <= (-right_data[1], right_data[2], right_data[0])


def test_limit_greater_than_n_returns_all(index) -> None:
    idx, scope = index
    hits = idx.search(scope, "backup job", limit=500)
    assert len(hits) == 120  # k > N → all rows, no crash, no dupes


def test_composite_scope_index_exists(index) -> None:
    idx, _scope_ = index
    row = idx.connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_memory_vectors_scope'"
    ).fetchone()
    assert row is not None, "composite scope index must exist (4.2)"


def test_scope_isolation_preserved_with_index(index, tmp_path: Path) -> None:
    idx, scope = index
    other = _scope().model_copy(update={"session_id": "other"})
    hits = idx.search(other, "backup job", limit=10)
    assert hits == []  # index must not leak across scopes


def test_dimension_validation_retained_on_read(index) -> None:
    """A row with a wrong-dimension blob still raises (4.1 guard)."""
    idx, scope = index
    from severian.domain.errors import VectorFormatError

    idx.connection.execute(
        "UPDATE memory_vectors SET vector = x'0000803F0000803F' WHERE memory_id = "
        "(SELECT memory_id FROM memory_vectors LIMIT 1)"
    )
    idx.connection.commit()
    with pytest.raises(VectorFormatError):
        idx.search(scope, "backup job", limit=10)
