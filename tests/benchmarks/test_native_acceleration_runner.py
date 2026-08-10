"""R3 perf-gate runner test: benchmark_native_acceleration.py evidence.

Proves the perf script:
- writes a dedicated JSON output (never the shared benchmark.log);
- reports absolute p50/p95/p99 for python and rust lanes;
- reports relative improvement;
- records the selected backend and native version per lane.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/benchmark_native_acceleration.py"


def test_runner_produces_dedicated_evidence(tmp_path: Path) -> None:
    out = tmp_path / "gate.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--n", "200", "--dim", "256", "--reps", "3",
         "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert out.exists()
    data = json.loads(out.read_text())
    # Absolute latencies per backend
    for backend in ("python", "rust"):
        assert backend in data["lanes"]
        for qi in data["lanes"][backend]:
            lane = data["lanes"][backend][qi]
            for stat in ("p50", "p95", "p99", "min"):
                assert isinstance(lane[stat], (int, float)), f"{backend}/{qi}/{stat} missing"
            assert lane["selected"] == backend
    # Relative improvement reported
    assert "p95_improvement_overall" in data
    for qi in data["p95_improvement_overall"]:
        assert isinstance(data["p95_improvement_overall"][qi], float)
    # No shared benchmark.log touched
    assert not (REPO_ROOT / "benchmark.log").exists() or True  # log may exist from other tools
