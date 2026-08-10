"""Single documented quantile method for all Severian benchmark evidence.

Every benchmark script (embedding bake-off, native acceleration gate,
vector-only Rust proof) must use THIS percentile implementation so a
reviewer can recompute p50/p95/p99 from the stored raw samples and get
bit-identical numbers. No off-by-one index arithmetic is allowed to
hide inside scripts (P1-H).

Method: linear interpolation between closest ranks — the NumPy
``linear`` default (a.k.a. type 7, R-7). For sorted samples of length
``n`` and percentile ``p`` in [0, 100]:

    rank = (n - 1) * p / 100
    value = samples[floor(rank)] * (1 - frac) + samples[ceil(rank)] * frac

where ``frac = rank - floor(rank)``. For n = 1 the single sample is
returned. Empty input is an error — a benchmark must never silently
emit 0.0 for a population it failed to measure.
"""
from __future__ import annotations

import math


def percentile(sorted_samples: list[float], p: float) -> float:
    """Linear-interpolation percentile over ASCENDING samples (type 7).

    ``sorted_samples`` must already be sorted ascending; the caller owns
    the sort so the method is O(1) per query after one sort. ``p`` must
    be in [0.0, 100.0]. Raises ValueError for empty input or invalid p.
    """
    if not sorted_samples:
        raise ValueError("percentile requires at least one sample")
    if not 0.0 <= p <= 100.0:
        raise ValueError(f"percentile p must be in [0, 100], got {p!r}")
    n = len(sorted_samples)
    if n == 1:
        return sorted_samples[0]
    rank = (n - 1) * (p / 100.0)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return sorted_samples[lo]
    frac = rank - lo
    return sorted_samples[lo] * (1.0 - frac) + sorted_samples[hi] * frac


def p50_p95_p99(sorted_samples: list[float]) -> tuple[float, float, float]:
    """Convenience: (p50, p95, p99) over ascending samples."""
    return (
        percentile(sorted_samples, 50.0),
        percentile(sorted_samples, 95.0),
        percentile(sorted_samples, 99.0),
    )


__all__ = ["p50_p95_p99", "percentile"]
