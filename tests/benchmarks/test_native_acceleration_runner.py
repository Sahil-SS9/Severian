"""R3 perf-gate runner test: benchmark_native_acceleration.py evidence.

P1-B contract:
- the benchmark test is NATIVE-ONLY: in a pure-Python install (no
  ``severian_native`` wheel) it must report an EXPLICIT skip, and the
  public fallback regression tests (tests/gates/
  test_native_fallback_contract.py) still execute;
- in a native install the test executes and passes — it must not skip;
- the script writes a dedicated JSON output (never the shared
  benchmark.log) — proven by running it in an ISOLATED workdir where
  an accidental benchmark.log would be visible and its absence can be
  asserted honestly (replaces the old vacuous ``assert ... or True``).

P1-H contract:
- raw per-query timing samples are stored so p50/p95/p99 are
  recomputable with the documented linear-interpolation method;
- exact candidate/source identity (git SHA + clean flag) is recorded;
- ID/order/score parity between python and rust lanes is recorded.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/benchmark_native_acceleration.py"

_NATIVE_AVAILABLE = True
try:  # pragma: no cover - import probe
    import severian_native  # noqa: F401

    _NATIVE_AVAILABLE = True
except Exception:  # noqa: BLE001 - optional wheel absent
    _NATIVE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _NATIVE_AVAILABLE,
    reason="severian_native not installed — native benchmark lane is skipped "
           "in pure-Python installs (public fallback contract is covered by "
           "tests/gates/test_native_fallback_contract.py)",
)


@pytest.fixture()
def gate_out(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    """Run the script once in an ISOLATED workdir; return (out_path, data)."""
    out = tmp_path / "gate.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--n", "200", "--dim", "256",
         "--reps", "3", "--out", str(out)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert out.exists()
    return out, json.loads(out.read_text())


def test_runner_produces_dedicated_evidence(tmp_path: Path) -> None:
    out, data = gate_out(tmp_path)
    # Absolute latencies per backend, recomputable from raw samples.
    for backend in ("python", "rust"):
        assert backend in data["lanes"]
        for qi in data["lanes"][backend]:
            lane = data["lanes"][backend][qi]
            for stat in ("p50", "p95", "p99", "min"):
                assert isinstance(lane[stat], (int, float)), f"{backend}/{qi}/{stat} missing"
            assert lane["selected"] == backend
            assert isinstance(lane["raw_ms"], list) and lane["raw_ms"], (
                f"{backend}/{qi} must store raw timing samples"
            )
            assert len(lane["raw_ms"]) == data["reps"]
    # Relative improvement reported
    assert "p95_improvement_overall" in data
    for qi in data["p95_improvement_overall"]:
        assert isinstance(data["p95_improvement_overall"][qi], float)
    # Dedicated output only: running in an isolated workdir makes an
    # accidental write to the shared benchmark.log detectable and
    # provably absent (the old `assert ... or True` was vacuous).
    assert not (tmp_path / "benchmark.log").exists(), (
        "script must never write the shared benchmark.log"
    )


def test_evidence_records_documented_quantile_method(tmp_path: Path) -> None:
    _, data = gate_out(tmp_path)
    assert data["quantile_method"].startswith("linear-interpolation"), data["quantile_method"]
    # Recompute p95 from raw samples with the documented method and the
    # stored value must match exactly (rounding to 4 dp recorded).
    from severian.benchmarks.quantiles import percentile

    for backend in data["lanes"]:
        for qi in data["lanes"][backend]:
            lane = data["lanes"][backend][qi]
            recomputed = round(percentile(sorted(lane["raw_ms"]), 95.0), 4)
            assert lane["p95"] == recomputed, (
                f"{backend}/{qi}: stored p95 {lane['p95']} != recomputed {recomputed}"
            )


def test_evidence_binds_exact_candidate_identity(tmp_path: Path) -> None:
    _, data = gate_out(tmp_path)
    source = data["source"]
    assert source["git_sha"], "evidence must record the candidate git SHA"
    assert source["git_sha"] == _git_head(), (
        "evidence git SHA must be the exact candidate HEAD"
    )
    assert source["clean"] == "true", "evidence must record clean worktree state"
    assert data["script_sha256"], "evidence must record the harness script hash"


def test_evidence_records_parity_and_parity_thresholds(tmp_path: Path) -> None:
    _, data = gate_out(tmp_path)
    overall = data["parity_overall"]
    assert overall["id_parity"] is True, "ID parity required (zero ranking difference)"
    assert overall["order_parity"] is True, "order parity required"
    assert overall["score_parity"] is True, "bit-exact score parity required"
    assert overall["max_score_diff"] == 0.0


def _git_head() -> str:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return result.stdout.strip()
