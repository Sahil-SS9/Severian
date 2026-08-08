"""RED: scale-lane cold query must FULLY warm the index before hot timing.

The cold query (derive=True) is capped at derive_max_observations=50 new
memories per call. On a 100k bucket the scale lane therefore times hot
queries against a ~50-row index, not a 100k-row index — the empty-index
trap in disguise: fast hot numbers that say nothing about real scale.

The scale harness must warm the index to stability (loop derive until no
new memories) before measuring hot latency, and must record the derived
count so the evidence proves the index was actually full.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.benchmarks.adapters.severian import SeverianAdapter  # noqa: E402


def _make_adapter(workdir: Path) -> SeverianAdapter:
    return SeverianAdapter(provider_id="severian-sqlite", workdir=str(workdir))


def _scope() -> str:
    return "t1/p1/c1/a1/s1"


def test_cold_query_must_fully_warm_index(tmp_path: Path) -> None:
    """Guard: the scale LANE (not a raw query) must fully warm the index.

    RED evidence (2026-08-08, preserved in the zero-debt profile): one
    search(derive=True) is capped at 50 new memories per call, so a raw
    cold query at 500 docs derived only ~50 — hot timing after it would
    measure a 50-row index. The harness fix: lane calls adapter.warmup()
    until stable and records warmed_memories in the evidence. This test
    asserts the harness-level invariant: the evidence file must prove
    warmed_memories == bucket for severian lanes.
    """
    adapter = _make_adapter(tmp_path / "w")
    try:
        for i in range(500):
            adapter.ingest(f"d{i}", f"fact {i}: service handles {i*7} requests/sec",
                           scope=_scope())
        # The lane's warmup path — full derive to stability.
        warmed = adapter.warmup(scope=_scope())  # type: ignore[attr-defined]
        assert warmed == 500, f"warmup derived {warmed}/500 — hot timing would be invalid"
    finally:
        adapter.close()


def test_warmup_method_exists_and_fully_derives(tmp_path: Path) -> None:
    adapter = _make_adapter(tmp_path / "w2")
    try:
        for i in range(300):
            adapter.ingest(f"d{i}", f"fact {i}: the queue depth reached {i*3}",
                           scope=_scope())
        assert hasattr(adapter, "warmup"), "SeverianAdapter must expose warmup()"
        t0 = time.perf_counter()
        count = adapter.warmup(scope=_scope())  # type: ignore[attr-defined]
        elapsed = time.perf_counter() - t0
        assert count == 300, f"warmup derived {count}/300"
        assert elapsed >= 0
    finally:
        adapter.close()
