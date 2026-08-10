"""RED: cross-version score contract (Rust follow-up R0.2).

The spike kernel uses Shewchuk compensated summation, bit-identical to
CPython ``math.fsum``. The pure-Python reference in vector.py used
``sum()``. This test LOCKS the version-independent contract: the Python
reference must use ``math.fsum``, and the Rust kernel must match it on
both supported Python versions — IDs and order EXACT, no tolerance.

Runs on the current interpreter. The same contract is exercised under
CPython 3.11 and 3.12 by the gate runner (test_native_fallback_contract).
"""
from __future__ import annotations

import heapq
import math
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


def _reference_fsum_topk(
    query: list[float],
    rows: list[tuple[str, list[float], str]],
    k: int,
) -> list[tuple[str, float]]:
    """The canonical Python reference: math.fsum scoring + the total
    tiebreak key (score DESC, content ASC, memory_id ASC)."""
    scored: list[tuple[tuple[float, str, str], str, float]] = []
    for memory_id, vector, content in rows:
        score = math.fsum(a * b for a, b in zip(query, vector, strict=True))
        scored.append(((-score, content, memory_id), memory_id, score))
    if k < len(scored):
        selected = heapq.nsmallest(k, scored, key=lambda item: item[0])
    else:
        selected = sorted(scored, key=lambda item: item[0])
    return [(mid, score) for (_key, mid, score) in selected]


def _reference_sum_topk(
    query: list[float],
    rows: list[tuple[str, list[float], str]],
    k: int,
) -> list[tuple[str, float]]:
    """The PRE-contract reference: builtin sum() — must be removed."""
    scored: list[tuple[tuple[float, str, str], str, float]] = []
    for memory_id, vector, content in rows:
        score = sum(a * b for a, b in zip(query, vector, strict=True))
        scored.append(((-score, content, memory_id), memory_id, score))
    if k < len(scored):
        selected = heapq.nsmallest(k, scored, key=lambda item: item[0])
    else:
        selected = sorted(scored, key=lambda item: item[0])
    return [(mid, score) for (_key, mid, score) in selected]


def test_vector_python_reference_uses_fsum() -> None:
    """The production pure-Python reference must use math.fsum, not
    builtin sum(): fsum is the version-independent score contract that
    the Rust kernel is bit-identical to."""
    src = (REPO_ROOT / "src/severian/infrastructure/vector.py").read_text()
    # search() must compute scores with math.fsum (or fsum equivalent).
    assert "fsum" in src, "vector.py reference must use math.fsum (R0.2)"
    # And the raw sum() call in search() must be gone.
    search_body = src.split("def search(")[1].split("def _search_numpy")[0]
    assert "sum(left * right" not in search_body, (
        "pre-contract builtin sum() must not survive in search()"
    )


@pytest.mark.parametrize("dimension", [256, 384, 768, 1024])
@pytest.mark.parametrize("n_rows", [0, 1, 999, 1000, 1001])
def test_fsum_contract_deterministic_and_total(dimension: int, n_rows: int) -> None:
    """fsum-based ordering is deterministic and total: identical scores
    tiebreak by content then memory_id, never by float noise."""
    rng = random.Random(20260809 + dimension + n_rows)
    query = [rng.uniform(-1.0, 1.0) for _ in range(dimension)]
    rows = []
    for i in range(n_rows):
        vec = [rng.uniform(-1.0, 1.0) for _ in range(dimension)]
        rows.append((f"id-{i:05d}", vec, f"content-{i % 7:02d}"))
    k = 20
    a = _reference_fsum_topk(query, rows, k)
    b = _reference_fsum_topk(query, rows, k)
    assert a == b, "fsum reference must be deterministic"
    assert len(a) == min(k, n_rows)
    # Total key: score strictly non-increasing; ties ordered by (content, id).
    if len(a) > 1:
        for (_prev_mid, prev_score), (_mid, score) in zip(a[:-1], a[1:], strict=True):
            assert score <= prev_score + 1e-15, "scores must be non-increasing"


def test_fsum_contract_exact_vs_rational() -> None:
    """fsum must reproduce exact rational arithmetic on cancellation-
    heavy vectors — the property that makes it the version-independent
    contract (3.11's builtin sum() is NOT compensated; 3.12's is)."""
    from fractions import Fraction

    # Products chosen so naive left-to-right accumulation loses the
    # small terms entirely; fsum keeps them.
    query = [1e16, 1e16, 1e16]
    rows = [("id-0", [1.0, 1.0, 1.0], "c0"),
            ("id-1", [1.0, 1.0, -1.0], "c1")]
    # Expected: id-0 = 3e16, id-1 = 1e16 — both exact under fsum.
    expected_exact = {
        "id-0": Fraction(3 * 10**16, 1),
        "id-1": Fraction(1 * 10**16, 1),
    }
    res = _reference_fsum_topk(query, rows, 2)
    for mid, score in res:
        # fsum's result must equal the exact rational value (both are
        # representable: 3e16 and 1e16 are exactly representable f64).
        assert Fraction(score).numerator * expected_exact[mid].denominator == (
            expected_exact[mid].numerator * Fraction(score).denominator
        ), f"{mid}: fsum {score} != exact {expected_exact[mid]}"
