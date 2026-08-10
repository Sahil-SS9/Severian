"""RED: the documented quantile method must be exact and recomputable.

P1-H: every benchmark stores raw samples and reports p50/p95/p99 with
ONE documented method (linear interpolation, NumPy type-7 default) so
reviewers can recompute the percentiles from the stored samples.
"""
from __future__ import annotations

import pytest

from severian.benchmarks.quantiles import p50_p95_p99, percentile


def test_single_sample() -> None:
    assert percentile([7.0], 95.0) == 7.0
    assert p50_p95_p99([7.0]) == (7.0, 7.0, 7.0)


def test_known_linear_case() -> None:
    # n=15, p95 -> rank = 14 * 0.95 = 13.3 -> v[13]*0.7 + v[14]*0.3
    samples = [float(i) for i in range(1, 16)]  # 1..15
    assert percentile(samples, 50.0) == 8.0
    assert percentile(samples, 95.0) == pytest.approx(14.3)
    assert percentile(samples, 99.0) == pytest.approx(14.86)
    p50, p95, p99 = p50_p95_p99(samples)
    assert p50 == 8.0
    assert p95 == pytest.approx(14.3)
    assert p99 == pytest.approx(14.86)


def test_matches_numpy_linear_if_available() -> None:
    """Cross-check against numpy.percentile(method='linear') when numpy
    is importable — the reference implementation of type 7."""
    import random

    rng = random.Random(42)
    samples = sorted(rng.uniform(0.0, 100.0) for _ in range(137))
    try:
        import numpy as np
    except ImportError:  # pragma: no cover - optional check
        return
    for p in (5.0, 25.0, 50.0, 90.0, 95.0, 99.0):
        expected = float(np.percentile(samples, p, method="linear"))
        assert percentile(samples, p) == pytest.approx(expected, abs=1e-12)


def test_input_contract() -> None:
    with pytest.raises(ValueError):
        percentile([], 95.0)
    with pytest.raises(ValueError):
        percentile([1.0], 101.0)
    with pytest.raises(ValueError):
        percentile([1.0], -1.0)
