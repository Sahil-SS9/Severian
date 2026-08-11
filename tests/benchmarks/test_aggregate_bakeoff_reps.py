"""Contract tests for aggregation of fresh-process embedding bake-off reps."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import aggregate_bakeoff_reps as aggregate  # noqa: E402


def _lane(
    provider_id: str,
    *,
    gate_passed: bool | None = True,
    skipped: bool = False,
    skip_reason: str = "",
    stale_fact_rate: float = 0.0,
    cross_scope_leakage: float = 0.0,
) -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "dimension": 384,
        "cold_load_seconds": 1.0,
        "peak_rss_mb": 100.0,
        "skipped": skipped,
        "skip_reason": skip_reason,
        "gate_passed": gate_passed,
        "gate_failures": [] if gate_passed else ["persisted_failure"],
        "quality": {
            "recall_at_10": 0.95,
            "ndcg_at_10": 0.95,
            "citation_precision": 0.90,
            "unsupported_answer_rate": 0.0,
            "negative_false_positive_rate": 0.0,
            "stale_fact_rate": stale_fact_rate,
            "cross_scope_leakage": cross_scope_leakage,
            "latency_p95_ms": 10.0,
        },
    }


def _write_rep(base: Path, number: int, lanes: list[dict[str, Any]]) -> None:
    rep = base / f"rep{number}"
    rep.mkdir(parents=True)
    (rep / "embedding-bakeoff.json").write_text(json.dumps({"lanes": lanes}))


def test_aggregate_requires_every_frozen_gate_and_every_rep_to_pass(tmp_path: Path) -> None:
    """Mean metrics must not hide a failed rep or omitted release gates."""
    for rep in (1, 2, 3):
        _write_rep(
            tmp_path,
            rep,
            [
                _lane("per-rep", gate_passed=rep != 2),
                _lane("stale", stale_fact_rate=0.1),
                _lane("cross-scope", cross_scope_leakage=0.1),
            ],
        )

    result = aggregate.aggregate(tmp_path)

    assert set(result["gate"]) == {
        "recall_at_10_min",
        "citation_precision_min",
        "unsupported_answer_rate_max",
        "negative_false_positive_rate_max",
        "stale_fact_rate_max",
        "cross_scope_leakage_max",
    }
    assert result["lanes"]["per-rep"]["rep_gate_passed"] == [True, False, True]
    assert result["lanes"]["per-rep"]["gate_passed"] is False
    assert "rep2:persisted_gate_failed" in result["lanes"]["per-rep"]["gate_failures"]
    assert result["lanes"]["stale"]["gate"]["stale_fact_rate"] == 0.1
    assert "stale_fact_rate" in result["lanes"]["stale"]["gate_failures"]
    assert result["lanes"]["cross-scope"]["gate"]["cross_scope_leakage"] == 0.1
    assert "cross_scope_leakage" in result["lanes"]["cross-scope"]["gate_failures"]


def test_aggregate_retains_skipped_lanes_and_blocks_selection_for_incomplete_matrix(tmp_path: Path) -> None:
    """A skipped mandatory lane must be visible and prevent a selection claim."""
    for rep in (1, 2, 3):
        _write_rep(
            tmp_path,
            rep,
            [
                _lane("winner"),
                _lane("gemma-300m", gate_passed=None, skipped=True, skip_reason="licence not authorised"),
            ],
        )

    result = aggregate.aggregate(tmp_path)

    assert result["lanes"]["gemma-300m"] == {
        "skipped": True,
        "rep_skip_reasons": ["licence not authorised"] * 3,
    }
    assert result["verdict"]["lanes_passing_gate"] == ["winner"]
    assert result["verdict"]["skipped_required_lanes"] == ["gemma-300m"]
    assert result["verdict"]["model_selection"] == "NO MODEL SELECTED"
