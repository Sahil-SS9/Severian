#!/usr/bin/env python3
"""Recompute Severian's October 2026 OmniMemEval headline scores from the archive.

No network, no model calls, no paid calls. Reads the per-question archive, verifies
its file manifest, rebuilds the judged rows, and compares the computed scores,
category counts/means and paired bootstrap ranges with
docs/benchmarks/omnimemeval/results-2026-10.json.

Usage:
    python docs/benchmarks/omnimemeval/recompute-2026-10.py \
        --archive ~/data/benchmarks/_records/omnimemeval-2026-10/results-per-question.tar.zst

    # If the archive is already extracted:
    python .../recompute-2026-10.py --extracted-root /tmp/omni-extract

    # Regenerate the committed file manifest from an extracted tree:
    python .../recompute-2026-10.py --extracted-root /tmp/omni-extract \
        --write-manifest docs/benchmarks/omnimemeval/MANIFEST-results-per-question-2026-10.sha256
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RESULTS = HERE / "results-2026-10.json"
MANIFEST = HERE / "MANIFEST-results-per-question-2026-10.sha256"
DEFAULT_ARCHIVE = Path(
    os.path.expanduser("~/data/benchmarks/_records/omnimemeval-2026-10/results-per-question.tar.zst")
)
ARCHIVE_SHA256 = "303e2486850257ad39a94315b3ceae480e5a36f92cc51803cfa29c1f928f34be"
REPS = 10_000
SEED = 20261023
SYSTEMS = ("severian", "lcmx")

LOCOMO_CATEGORIES = {4: "Single-Hop", 1: "Multi-Hop", 2: "Temporal", 3: "Open-Domain"}
LME_CATEGORIES = {
    "single-session-user": "SS-User",
    "single-session-assistant": "SS-Asst",
    "single-session-preference": "SS-Pref",
    "temporal-reasoning": "Temp. Reas",
    "multi-session": "Multi-S",
    "knowledge-update": "Know. Upd",
}
BEAM_CATEGORIES = {
    "abstention": "Abstention",
    "contradiction_resolution": "Contradiction",
    "event_ordering": "Event ordering",
    "information_extraction": "Info extraction",
    "instruction_following": "Instruction following",
    "knowledge_update": "Knowledge update",
    "multi_session_reasoning": "Multi-session",
    "preference_following": "Preference following",
    "summarization": "Summarisation",
    "temporal_reasoning": "Temporal",
}


def _lit(value: Any) -> Any:
    return ast.literal_eval(value) if isinstance(value, str) else value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _root_from_extracted(path: Path) -> Path:
    if (path / "h2h-p1/omni/results").is_dir():
        return path
    if (path / "omni/results").is_dir():
        return path.parent
    raise SystemExit(f"cannot find h2h-p1/omni/results under {path}")


def extract_archive(archive: Path, target: Path) -> Path:
    if not archive.is_file():
        raise SystemExit(f"archive not found: {archive}")
    actual = _sha256(archive)
    if actual != ARCHIVE_SHA256:
        raise SystemExit(f"archive sha256 mismatch: expected {ARCHIVE_SHA256}, got {actual}")
    print(f"archive sha256 OK: {actual}")
    subprocess.run(["tar", "--zstd", "-xf", str(archive), "-C", str(target)], check=True)
    return _root_from_extracted(target)


def write_manifest(root: Path, out: Path) -> int:
    files = sorted(path for path in root.rglob("*") if path.is_file())
    lines = [f"{_sha256(path)}  {path.relative_to(root).as_posix()}" for path in files]
    out.write_text("\n".join(lines) + "\n")
    print(f"wrote {out} ({len(lines)} files, sha256 {_sha256(out)})")
    return len(lines)


def verify_manifest(root: Path, manifest: Path) -> int:
    rows = []
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        path = root / rel
        if not path.is_file():
            raise SystemExit(f"manifest file missing: {rel}")
        actual = _sha256(path)
        if actual != digest:
            raise SystemExit(f"manifest mismatch: {rel}")
        rows.append(rel)
    print(f"manifest OK: {len(rows)} files, sha256 {_sha256(manifest)}")
    return len(rows)


def board_rows(results_root: Path, bench: str, system: str) -> dict[tuple[str, Any], tuple[str, float]]:
    filename = results_root / bench / f"{system}-p1" / f"{system}_{bench}_judged.json"
    payload = json.loads(filename.read_text())
    rows: dict[tuple[str, Any], tuple[str, float]] = {}
    for uid, group in payload.items():
        records = [group] if isinstance(group, dict) else group
        for index, record in enumerate(records):
            if "llm_judgments" in record:
                marks = _lit(record["llm_judgments"]) or {}
                score = sum(float(value) for value in marks.values()) / len(marks)
            else:
                score = float(record["nugget_score"])
            key = record.get("key", index)
            rows[(uid, key)] = (" ".join(str(record.get("question", "")).split()).lower(), score)
    return rows


def paired(left: dict[tuple[str, Any], tuple[str, float]], right: dict[tuple[str, Any], tuple[str, float]]):
    keys = sorted(set(left) & set(right))
    if set(left) != set(right):
        raise SystemExit("the two systems were asked different question sets")
    bad = [key for key in keys if left[key][0] != right[key][0]]
    if bad:
        raise SystemExit(f"{len(bad)} pairs have different question text, e.g. {bad[:2]}")
    diffs = [left[key][1] - right[key][1] for key in keys]
    n = len(diffs)
    mean = sum(diffs) / n
    rnd = random.Random(SEED)
    boots = sorted(sum(diffs[rnd.randrange(n)] for _ in range(n)) / n for _ in range(REPS))
    lo = boots[int(0.025 * REPS)]
    hi = boots[int(0.975 * REPS)]
    wins = sum(diff > 0 for diff in diffs)
    losses = sum(diff < 0 for diff in diffs)
    return n, mean, lo, hi, wins, losses


def bench_data(results_root: Path, bench: str):
    left = board_rows(results_root, bench, "severian")
    right = board_rows(results_root, bench, "lcmx")
    return left, right


def category_scores(bench: str, left, right, results_root: Path):
    if bench == "locomo":
        payload = json.loads((results_root / bench / "severian-p1" / "severian_locomo_judged.json").read_text())
        cats = {}
        right_cats = {}
        for uid, records in payload.items():
            for index, record in enumerate(records):
                cats[(uid, index)] = LOCOMO_CATEGORIES[int(record["category"])]
        payload = json.loads((results_root / bench / "lcmx-p1" / "lcmx_locomo_judged.json").read_text())
        for uid, records in payload.items():
            for index, record in enumerate(records):
                right_cats[(uid, index)] = LOCOMO_CATEGORIES[int(record["category"])]
    elif bench == "lme":
        cats = {}
        right_cats = {}
        payload = json.loads((results_root / bench / "severian-p1" / "severian_lme_judged.json").read_text())
        cats = {(uid, 0): LME_CATEGORIES[record["category"]] for uid, record in payload.items()}
        payload = json.loads((results_root / bench / "lcmx-p1" / "lcmx_lme_judged.json").read_text())
        right_cats = {(uid, 0): LME_CATEGORIES[record["category"]] for uid, record in payload.items()}
    else:
        cats = {}
        right_cats = {}
        payload = json.loads((results_root / bench / "severian-p1" / "severian_beam_judged.json").read_text())
        cats = {
            (conv, record["key"]): BEAM_CATEGORIES[record["dimension"]]
            for conv, records in payload.items()
            for record in records
        }
        payload = json.loads((results_root / bench / "lcmx-p1" / "lcmx_beam_judged.json").read_text())
        right_cats = {
            (conv, record["key"]): BEAM_CATEGORIES[record["dimension"]]
            for conv, records in payload.items()
            for record in records
        }
    grouped = {}
    for key in sorted(set(left) & set(right)):
        name = cats.get(key)
        if name is None:
            raise SystemExit(f"missing category for {key}")
        grouped.setdefault(name, []).append((left[key][1], right[key][1]))
    return [
        (
            name,
            len(values),
            100 * sum(a for a, _ in values) / len(values),
            100 * sum(b for _, b in values) / len(values),
        )
        for name, values in grouped.items()
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--extracted-root", type=Path, help="already-extracted archive root (saves temp extraction)")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--results", type=Path, default=RESULTS)
    parser.add_argument(
        "--write-manifest", type=Path, metavar="PATH", help="write the file manifest from the tree and exit"
    )
    args = parser.parse_args(argv)

    temp = None
    if args.extracted_root:
        root = _root_from_extracted(args.extracted_root)
    else:
        temp = Path(tempfile.mkdtemp(prefix="omni-recompute-", dir="/tmp"))
        root = extract_archive(args.archive, temp)
    try:
        if args.write_manifest:
            count = write_manifest(root, args.write_manifest)
            if count <= 0:
                raise SystemExit("empty manifest")
            return 0
        verify_manifest(root, args.manifest)
        expected = json.loads(args.results.read_text())
        results_root = root / "h2h-p1/omni/results"
        tolerance = 0.006
        ci_tolerance = 0.011
        failures = []
        print("bench        n     Severian  LCM-X    diff      95% range       sev/lcmx only")
        for bench in ("locomo", "lme", "beam"):
            left, right = bench_data(results_root, bench)
            n, mean, lo, hi, wins, losses = paired(left, right)
            sev = 100 * sum(score for _, score in left.values()) / len(left)
            lcm = 100 * sum(score for _, score in right.values()) / len(right)
            expected_bench = expected["head_to_head"][bench]
            checks = {
                "n": (n, expected_bench["n"]),
                "severian": (round(sev, 2), expected_bench["severian"]),
                "lcmx": (round(lcm, 2), expected_bench["lcmx"]),
                "diff": (round(100 * mean, 2), expected_bench["diff"]),
                "ci_lo": (round(100 * lo, 2), expected_bench["ci"][0]),
                "ci_hi": (round(100 * hi, 2), expected_bench["ci"][1]),
                "sev_only": (wins, expected_bench["sev_only"]),
                "lcmx_only": (losses, expected_bench["lcmx_only"]),
            }
            for name, (actual, wanted) in checks.items():
                if name == "n" or name.startswith("sev_only") or name.startswith("lcmx_only"):
                    ok = actual == wanted
                elif name.startswith("ci_"):
                    ok = abs(actual - wanted) <= ci_tolerance
                else:
                    ok = abs(actual - wanted) <= tolerance
                if not ok:
                    failures.append(f"{bench}.{name}: computed {actual!r}, expected {wanted!r}")
            print(
                f"{bench:10s} {n:5d} {sev:9.2f} {lcm:7.2f} {100 * mean:+7.2f}  "
                f"[{100 * lo:+.2f}, {100 * hi:+.2f}]  {wins:3d}/{losses:3d}"
            )
            for name, count, cat_sev, cat_lcm in category_scores(bench, left, right, results_root):
                wanted = expected_bench["categories"][name]
                if (
                    count != wanted[0]
                    or abs(cat_sev - wanted[1]) > tolerance
                    or abs(cat_lcm - wanted[2]) > tolerance
                ):
                    failures.append(
                        f"{bench}.category.{name}: got "
                        f"{(count, round(cat_sev, 2), round(cat_lcm, 2))}, expected {wanted}"
                    )
                print(f"  {name:24s} n={count:4d}  {cat_sev:6.2f}  {cat_lcm:6.2f}")
        if failures:
            print("MISMATCHES:")
            for failure in failures:
                print(" -", failure)
            return 1
        print("PASS: 87.34 / 90.60 / 71.62 and all paired ranges and category rows match results-2026-10.json")
        return 0
    finally:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
