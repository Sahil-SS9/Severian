"""GREEN: native package contract (Rust follow-up plan R1.3).

Parity matrix between the pure-Python fsum reference and the
`severian_native.exact_top_k` kernel:

- dimensions: 256, 384, 512, 768, 1024
- rows: 0, 1, k-1, k, k+1, 1K, 10K and bounded 100K
- k: 0, 1, 20, N, N+1
- random vectors; identical vectors and exact score ties
- repeated deterministic runs
- malformed lengths; NaN/Inf in queries and rows
- non-ASCII IDs/content

IDs and order must be EXACT; scores must be bit-identical (both sides
use Shewchuk fsum). No tolerance that can change ranking.
"""
from __future__ import annotations

import heapq
import math
import random
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.infrastructure.vector import _unpack_vector  # noqa: E402

severian_native = pytest.importorskip(
    "severian_native", reason="severian-native wheel not installed"
)


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _reference_topk(
    query: list[float],
    rows: list[tuple[str, str, bytes]],
    k: int,
) -> list[tuple[str, float]]:
    """Exact mirror of LocalVectorIndex.search's pure-Python path:
    _unpack_vector + math.fsum dot + heapq.nsmallest with the total key
    (score DESC, content ASC, memory_id ASC)."""
    scored: list[tuple[tuple[float, str, str], str, float]] = []
    for memory_id, content, blob in rows:
        vector = _unpack_vector(blob, len(query))
        score = math.fsum(
            a * b for a, b in zip(query, vector, strict=True)
        )
        scored.append(((-score, content, memory_id), memory_id, score))
    if k < len(scored):
        selected = heapq.nsmallest(k, scored, key=lambda item: item[0])
    else:
        selected = sorted(scored, key=lambda item: item[0])
    return [(mid, score) for (_key, mid, score) in selected]


def _native_topk(
    query: list[float],
    rows: list[tuple[str, str, bytes]],
    k: int,
) -> list[tuple[str, float]]:
    native_rows = [(mid, content, blob) for mid, content, blob in rows]
    result = severian_native.exact_top_k(query, native_rows, k)
    return [(mid, float(score)) for mid, score in result]


def _rand_rows(n: int, dim: int, seed: int) -> tuple[list[float], list[tuple[str, str, bytes]]]:
    rng = random.Random(seed)
    query = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
    rows = []
    for i in range(n):
        vector = [rng.uniform(-1.0, 1.0) for _ in range(dim)]
        rows.append((f"mem-{i:08d}", f"content-{i % 11:03d}", _pack(vector)))
    return query, rows


@pytest.mark.parametrize("dim", [256, 384, 512, 768, 1024])
@pytest.mark.parametrize("n", [0, 1, 19, 20, 21, 1000, 10000])
def test_parity_exact_ids_order_scores(dim: int, n: int) -> None:
    """Full parity matrix: IDs, order and bit-exact scores."""
    query, rows = _rand_rows(n, dim, seed=dim * 1000 + n)
    k = 20
    ref = _reference_topk(query, rows, k)
    nat = _native_topk(query, rows, k)
    assert [mid for mid, _ in ref] == [mid for mid, _ in nat], (
        f"ID/order mismatch dim={dim} n={n}"
    )
    for (mid_ref, score_ref), (mid_nat, score_nat) in zip(ref, nat, strict=True):
        assert mid_ref == mid_nat
        assert score_ref == score_nat, (
            f"score mismatch dim={dim} n={n}: {score_ref!r} != {score_nat!r}"
        )


def test_parity_bounded_100k() -> None:
    """Bounded 100K rows (subset sampled — full 100K is a perf lane)."""
    dim = 384
    n = 100_000
    query, rows = _rand_rows(n, dim, seed=424242)
    k = 20
    ref = _reference_topk(query, rows, k)
    nat = _native_topk(query, rows, k)
    assert [mid for mid, _ in ref] == [mid for mid, _ in nat]
    for (_, s1), (_, s2) in zip(ref, nat, strict=True):
        assert s1 == s2


@pytest.mark.parametrize("k", [0, 1, 5, 20, 1000, 1001])
def test_parity_k_values(k: int) -> None:
    """k=0, k=1, k<N, k=N, k>N defined identically."""
    dim, n = 384, 1000
    query, rows = _rand_rows(n, dim, seed=7)
    ref = _reference_topk(query, rows, k)
    nat = _native_topk(query, rows, k)
    assert [mid for mid, _ in ref] == [mid for mid, _ in nat]
    for (_, s1), (_, s2) in zip(ref, nat, strict=True):
        assert s1 == s2


def test_parity_identical_vectors_and_exact_ties() -> None:
    """All-identical vectors + exact score ties: content then id order."""
    dim, n, k = 64, 50, 10
    query = [0.5] * dim
    rows = []
    for i in range(n):
        rows.append((f"mem-{i:04d}", f"c{i % 3:02d}", _pack([0.5] * dim)))
    ref = _reference_topk(query, rows, k)
    nat = _native_topk(query, rows, k)
    assert [mid for mid, _ in ref] == [mid for mid, _ in nat]
    # All scores identical — ordering must come from (content, id) only.
    scores = {s for _, s in ref}
    assert len(scores) == 1


def test_parity_repeated_deterministic_runs() -> None:
    dim, n, k = 384, 5000, 20
    query, rows = _rand_rows(n, dim, seed=99)
    first = _native_topk(query, rows, k)
    for _ in range(3):
        assert _native_topk(query, rows, k) == first


def test_parity_non_ascii_ids_and_content() -> None:
    dim, n, k = 128, 200, 10
    query, rows = _rand_rows(n, dim, seed=5)
    rows = [
        (f"mem-ü-{i}", f"内容-{i % 9}", blob)
        for i, (_mid, _content, blob) in enumerate(rows)
    ]
    ref = _reference_topk(query, rows, k)
    nat = _native_topk(query, rows, k)
    assert [mid for mid, _ in ref] == [mid for mid, _ in nat]


def test_parity_adversarial_cancellation_matches_cpython_fsum() -> None:
    """The native final partial collapse must match CPython's half-even fsum.

    A random unit-vector matrix does not exercise the critical rounding path.
    This ordered magnitude sequence does: plain left-to-right summation loses
    the unit term, while ``math.fsum`` recovers the correctly rounded result.
    """
    query = [1e-16, 1.0, 1e16]
    rows = [("mem", "cancellation", _pack([1.0, 1.0, 1.0]))]

    expected = _reference_topk(query, rows, 1)
    actual = _native_topk(query, rows, 1)

    assert expected[0][1] == math.fsum(query)
    assert actual == expected


def test_error_contract_malformed_lengths() -> None:
    """Malformed BLOBs raise a typed error; no panic/segfault."""
    dim, n = 64, 10
    query, rows = _rand_rows(n, dim, seed=3)
    bad = rows[:-1] + [("mem-bad", "content", _pack([0.5] * (dim - 1)))]
    with pytest.raises(ValueError):
        severian_native.exact_top_k(query, bad, 5)
    # Query dimension mismatch
    short_query = [0.0] * (dim - 1)
    with pytest.raises(ValueError):
        severian_native.exact_top_k(short_query, rows, 5)
    # Empty query
    with pytest.raises(ValueError):
        severian_native.exact_top_k([], rows, 5)


def test_error_contract_nan_inf() -> None:
    dim, n, k = 64, 10, 5
    query, rows = _rand_rows(n, dim, seed=11)
    nan_q = [float("nan")] + [0.0] * (dim - 1)
    with pytest.raises(ValueError):
        severian_native.exact_top_k(nan_q, rows, k)
    inf_rows = rows[:-1] + [("mem-inf", "c", _pack([float("inf")] * dim))]
    with pytest.raises(ValueError):
        severian_native.exact_top_k(query, inf_rows, k)


def test_error_contract_rejects_overflowed_dot_product() -> None:
    """Finite components must not produce a ranked ``inf`` score."""
    max_f32 = 3.4028234663852886e38
    with pytest.raises(ValueError, match="non-finite dot product"):
        severian_native.exact_top_k(
            [1e308], [("mem", "overflow", _pack([max_f32]))], 1
        )


def test_empty_rows_and_k0() -> None:
    query = [0.5, -0.5, 1.0]
    assert severian_native.exact_top_k(query, [], 5) == []
    query, rows = _rand_rows(10, 64, seed=1)
    assert severian_native.exact_top_k(query, rows, 0) == []


def test_native_version_diagnostics() -> None:
    v = severian_native.native_version()
    assert v.startswith("severian-native-0.1.0")
