#!/usr/bin/env python3
"""Aggregate fresh-process embedding bake-off reps into decision evidence.

The raw ``embedding-bakeoff.json`` summary is authoritative for lane inventory.
Using it preserves licence-gated skipped lanes that intentionally have no separate
``lane-<provider>.json`` file. A lane passes only when every rep persisted a pass
and every frozen aggregate gate passes; means may describe metrics, never hide a
failed rep.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPS = (1, 2, 3)
GATE = {
    "recall_at_10_min": 0.90,
    "citation_precision_min": 0.78,
    "unsupported_answer_rate_max": 0.10,
    "negative_false_positive_rate_max": 0.0,
    "stale_fact_rate_max": 0.0,
    "cross_scope_leakage_max": 0.0,
}

_GATE_RULES = (
    ("recall_at_10", "recall_at_10_min", ">="),
    ("citation_precision", "citation_precision_min", ">="),
    ("unsupported_answer_rate", "unsupported_answer_rate_max", "<="),
    ("negative_false_positive_rate", "negative_false_positive_rate_max", "<="),
    ("stale_fact_rate", "stale_fact_rate_max", "<="),
    ("cross_scope_leakage", "cross_scope_leakage_max", "<="),
)
_SUMMARY_METRICS = (
    "recall_at_10",
    "ndcg_at_10",
    "latency_p95_ms",
    "cold_load_seconds",
    "peak_rss_mb",
    "dimension",
)


def _summary_path(base: Path, rep: int) -> Path:
    return base / f"rep{rep}" / "embedding-bakeoff.json"


def _load_lanes(base: Path, rep: int) -> dict[str, dict[str, Any]]:
    path = _summary_path(base, rep)
    payload = json.loads(path.read_text())
    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        raise ValueError(f"{path} has no lane list")

    by_id: dict[str, dict[str, Any]] = {}
    for lane in lanes:
        if not isinstance(lane, dict):
            raise ValueError(f"{path} contains a non-object lane")
        provider_id = lane.get("provider_id")
        if not isinstance(provider_id, str) or not provider_id:
            raise ValueError(f"{path} contains a lane without provider_id")
        if provider_id in by_id:
            raise ValueError(f"{path} contains duplicate lane {provider_id}")
        by_id[provider_id] = lane
    return by_id


def _mean(values: list[float]) -> float:
    return round(sum(values) / len(values), 4)


def _metric_values(rows: list[dict[str, Any]]) -> dict[str, float]:
    values: dict[str, float] = {}
    for metric, _threshold, _operator in _GATE_RULES:
        values[metric] = _mean([float(row["quality"][metric]) for row in rows])
    return values


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for metric in _SUMMARY_METRICS:
        if metric in {"recall_at_10", "ndcg_at_10", "latency_p95_ms"}:
            values = [float(row["quality"][metric]) for row in rows]
        else:
            values = [float(row[metric]) for row in rows]
        summary[metric] = {
            "mean": _mean(values),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
        }
    return summary


def _gate_failures(
    gate_values: dict[str, float],
    rep_passes: list[bool],
    throughput_passes: list[bool],
) -> list[str]:
    failures: list[str] = []
    for metric, threshold, operator in _GATE_RULES:
        value = gate_values[metric]
        limit = GATE[threshold]
        passes = value >= limit if operator == ">=" else value <= limit
        if not passes:
            failures.append(metric)
    failures.extend(
        f"rep{rep}:persisted_gate_failed"
        for rep, passed in zip(REPS, rep_passes, strict=True)
        if not passed
    )
    failures.extend(
        f"rep{rep}:throughput_incomplete"
        for rep, complete in zip(REPS, throughput_passes, strict=True)
        if not complete
    )
    return failures


def aggregate(base: Path) -> dict[str, Any]:
    """Return deterministic all-gates/all-reps aggregation for ``base``."""
    reps = {rep: _load_lanes(base, rep) for rep in REPS}
    expected_ids = set(reps[REPS[0]])
    for rep, lanes in reps.items():
        lane_ids = set(lanes)
        if lane_ids != expected_ids:
            missing = sorted(expected_ids - lane_ids)
            extra = sorted(lane_ids - expected_ids)
            raise ValueError(f"rep{rep} lane matrix mismatch: missing={missing} extra={extra}")

    lanes_out: dict[str, dict[str, Any]] = {}
    for provider_id in sorted(expected_ids):
        rows = [reps[rep][provider_id] for rep in REPS]
        skipped = [bool(row.get("skipped")) for row in rows]
        if any(skipped):
            if not all(skipped):
                raise ValueError(f"{provider_id} skip state differs across reps")
            lanes_out[provider_id] = {
                "skipped": True,
                "rep_skip_reasons": [str(row.get("skip_reason", "")) for row in rows],
            }
            continue

        rep_passes = [row.get("gate_passed") is True for row in rows]
        throughput_passes = [row.get("throughput_complete") is True for row in rows]
        throughput_errors = [str(row.get("throughput_error", "")) for row in rows]
        gate_values = _metric_values(rows)
        failures = _gate_failures(gate_values, rep_passes, throughput_passes)
        lane = {
            "skipped": False,
            "n": len(rows),
            **_metric_summary(rows),
            "gate": gate_values,
            "rep_gate_passed": rep_passes,
            "rep_throughput_complete": throughput_passes,
            "throughput_complete": all(throughput_passes),
            "throughput_errors": throughput_errors,
            "gate_failures": failures,
            "gate_passed": not failures,
        }
        lanes_out[provider_id] = lane

    passed = [
        provider_id
        for provider_id, lane in lanes_out.items()
        if not lane["skipped"] and lane["gate_passed"]
    ]
    skipped_required = [provider_id for provider_id, lane in lanes_out.items() if lane["skipped"]]
    incomplete_matrix = bool(skipped_required)
    model_selection = "NO MODEL SELECTED" if incomplete_matrix or not passed else ",".join(passed)

    return {
        "kind": "embedding-bakeoff-aggregate",
        "reps": list(REPS),
        "gate": GATE,
        "lanes": lanes_out,
        "verdict": {
            "model_selection": model_selection,
            "lanes_passing_gate": passed,
            "skipped_required_lanes": skipped_required,
            "note": (
                "NO SELECTION unless every required lane is present and a lane clears "
                "all six frozen metrics in every fresh-process rep."
            ),
        },
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: aggregate_bakeoff_reps.py <bakeoff-reps-directory>")
    print(json.dumps(aggregate(Path(sys.argv[1])), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
