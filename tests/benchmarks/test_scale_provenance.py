"""RED: scale-lane evidence must carry full provenance (quality rule 4).

The zero-debt plan requires every benchmark record to contain:
candidate SHA, dirty-patch identity if any, corpus/manifest hash, exact
model revision, runtime versions, hardware fingerprint, command, seed,
repetitions and skipped lanes.

Current state: scripts/run_scale_benchmark.py records git_sha, corpus
hash and seed only. This test proves the missing fields before the
script is extended. The same requirement applies to the dreaming scale
lane (scripts/run_dreaming_scale.py).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "run_scale_benchmark.py"

REQUIRED_PROVENANCE = {
    "repro": {
        "git_sha",
        "dirty_patch",           # missing today
        "generated_corpus_sha256",
        "model_revision",        # missing today (hash baseline = "n/a")
        "runtime_versions",      # missing today
        "hardware",              # missing today
        "command",               # missing today
    },
    "top_level": {
        "repetitions",           # missing today
        "skipped_lanes",
    },
}

# seed is already recorded at top level — the requirement is that it is
# recorded somewhere in the evidence, not at a fixed nesting.
_ANYWHERE = {"seed"}


def _run_tiny_scale(out_dir: Path) -> dict[str, Any]:
    """Run the scale script at the minimum bucket with sqlite lane only."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--min-bucket",
            "1000",
            "--max-bucket",
            "1000",
            "--out",
            str(out_dir),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"scale script failed rc={result.returncode}\n"
        f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
    )
    payload: dict[str, Any] = json.loads((out_dir / "scale.json").read_text())
    return payload


def _run_tiny_dreaming(out_dir: Path) -> dict[str, Any]:
    """Run the dreaming scale script at the minimum bucket."""
    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "run_dreaming_scale.py"),
            "--max-bucket",
            "1000",
            "--out",
            str(out_dir / "dreaming-scale.json"),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=600,
    )
    assert result.returncode == 0, (
        f"dreaming script failed rc={result.returncode}\n"
        f"stdout: {result.stdout[-2000:]}\nstderr: {result.stderr[-2000:]}"
    )
    payload: dict[str, Any] = json.loads((out_dir / "dreaming-scale.json").read_text())
    return payload


def test_evidence_records_all_required_provenance(tmp_path: Path) -> None:
    payload = _run_tiny_scale(tmp_path / "out")

    missing = sorted(
        key
        for key in REQUIRED_PROVENANCE["repro"]
        if key not in payload.get("repro", {})
    )
    assert not missing, f"repro provenance fields missing: {missing}"

    missing_top = sorted(
        key
        for key in REQUIRED_PROVENANCE["top_level"]
        if key not in payload
    )
    assert not missing_top, f"top-level evidence fields missing: {missing_top}"

    serialized = json.dumps(payload, sort_keys=True)
    missing_anywhere = sorted(
        key for key in _ANYWHERE if key not in serialized
    )
    assert not missing_anywhere, f"evidence fields missing everywhere: {missing_anywhere}"

    # Values must be non-trivial, not placeholder strings.
    assert payload["repro"]["runtime_versions"], "runtime_versions empty"
    assert payload["repro"]["hardware"], "hardware empty"
    assert payload["repro"]["command"], "command empty"
    assert payload["repro"]["model_revision"], "model_revision empty"
    assert payload["repetitions"] >= 1, "repetitions must be >= 1"


def test_dreaming_evidence_records_all_required_provenance(tmp_path: Path) -> None:
    payload = _run_tiny_dreaming(tmp_path / "dream")

    missing = sorted(
        key
        for key in REQUIRED_PROVENANCE["repro"]
        if key not in payload.get("repro", {})
    )
    assert not missing, f"dreaming repro provenance fields missing: {missing}"

    assert payload["repro"]["runtime_versions"], "runtime_versions empty"
    assert payload["repro"]["hardware"], "hardware empty"
    assert payload["repro"]["command"], "command empty"
    assert payload["repro"]["model_revision"], "model_revision empty"
