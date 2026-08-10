#!/usr/bin/env python3
"""Aggregate the 3 fresh-process bake-off reps into durable evidence.

Deterministic: reads rep{1,2,3}/embedding-bakeoff.json, computes per-lane
mean/min/max across reps for recall@10, nDCG@10, p95ms, cold_s, RSS_MB,
and applies the frozen gate (recall>=0.90, citation>=0.78,
unsupported<=0.10, negative-FP<=0.0). Prints a single JSON document.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPS = [1, 2, 3]
GATE = {
    "recall_at_10_min": 0.90,
    "citation_precision_min": 0.78,
    "unsupported_answer_rate_max": 0.10,
    "negative_false_positive_rate_max": 0.0,
}


def load_lane(rep_dir: Path, lane_id: str) -> dict:
    p = rep_dir / f"lane-{lane_id}.json"
    return json.loads(p.read_text())


def main() -> int:
    base = Path(sys.argv[1])
    lanes = {}
    # discover lane ids from rep1
    rep1 = base / "rep1"
    lane_files = sorted(rep1.glob("lane-*.json"))
    for lf in lane_files:
        lane_id = lf.stem[len("lane-"):]
        rows = []
        for r in REPS:
            p = base / f"rep{r}" / f"lane-{lane_id}.json"
            if not p.exists():
                raise SystemExit(f"missing {p}")
            rows.append(json.loads(p.read_text()))
        lanes[lane_id] = rows

    out = {"kind": "embedding-bakeoff-aggregate", "reps": REPS,
           "gate": GATE, "lanes": {}, "verdict": {}}
    for lane_id, rows in sorted(lanes.items()):
        if rows[0].get("skipped") or rows[0].get("skip_reason"):
            out["lanes"][lane_id] = {"skipped": True,
                                     "skip_reason": rows[0].get("skip_reason", "")}
            continue
        agg = {"skipped": False, "n": len(rows)}
        for metric in ("recall_at_10", "ndcg_at_10", "latency_p95_ms",
                       "cold_load_seconds", "peak_rss_mb", "dimension"):
            vals = []
            for row in rows:
                if metric == "recall_at_10":
                    v = row["quality"]["recall_at_10"]
                elif metric == "ndcg_at_10":
                    v = row["quality"]["ndcg_at_10"]
                elif metric == "latency_p95_ms":
                    v = row["quality"]["latency_p95_ms"]
                elif metric == "cold_load_seconds":
                    v = row["cold_load_seconds"]
                elif metric == "peak_rss_mb":
                    v = row["peak_rss_mb"]
                else:
                    v = row["dimension"]
                vals.append(float(v))
            agg[metric] = {
                "mean": round(sum(vals) / len(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
            }
        # gate check on mean recall + citation + unsupported + negative-FP
        q = rows[0]["quality"]
        agg["gate"] = {
            "recall_at_10": round(sum(r["quality"]["recall_at_10"] for r in rows) / len(rows), 4),
            "citation_precision": round(sum(r["quality"]["citation_precision"] for r in rows) / len(rows), 4),
            "unsupported_answer_rate": round(sum(r["quality"]["unsupported_answer_rate"] for r in rows) / len(rows), 4),
            "negative_false_positive_rate": round(sum(r["quality"]["negative_false_positive_rate"] for r in rows) / len(rows), 4),
        }
        g = agg["gate"]
        passes = (g["recall_at_10"] >= GATE["recall_at_10_min"]
                  and g["citation_precision"] >= GATE["citation_precision_min"]
                  and g["unsupported_answer_rate"] <= GATE["unsupported_answer_rate_max"]
                  and g["negative_false_positive_rate"] <= GATE["negative_false_positive_rate_max"])
        agg["gate_passed"] = passes
        out["lanes"][lane_id] = agg

    passed = [lid for lid, v in out["lanes"].items()
              if not v.get("skipped") and v.get("gate_passed")]
    out["verdict"] = {
        "model_selection": "NO MODEL SELECTED" if not passed else ",".join(passed),
        "lanes_passing_gate": passed,
        "note": "NO SELECTION unless a lane clears ALL four frozen metrics "
                "across all 3 fresh-process reps; gemma-300m licence-gated "
                "SKIPPED -> incomplete matrix -> NO SELECTION by locked decision.",
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
