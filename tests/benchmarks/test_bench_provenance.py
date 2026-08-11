"""Benchmark evidence provenance must identify dirty source bytes exactly."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_provenance import collect_provenance  # noqa: E402


def _run(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "init", "-q")
    _run(repo, "config", "user.email", "tests@example.invalid")
    _run(repo, "config", "user.name", "Severian tests")
    (repo / "tracked.txt").write_text("base\n")
    _run(repo, "add", "tracked.txt")
    _run(repo, "commit", "-qm", "baseline")
    return repo


def test_dirty_patch_hash_tracks_changed_bytes_not_only_paths(tmp_path: Path) -> None:
    """Two edits to the same tracked path must have different provenance hashes."""
    repo = _committed_repo(tmp_path)
    tracked = repo / "tracked.txt"

    tracked.write_text("first variant\n")
    first = collect_provenance(repo, ["benchmark"], seed=1)
    tracked.write_text("second variant\n")
    second = collect_provenance(repo, ["benchmark"], seed=1)

    assert first["dirty_patch"] == second["dirty_patch"]
    assert first["dirty_patch"] != "clean"
    assert first["dirty_patch_sha256"] != second["dirty_patch_sha256"]


def test_dirty_patch_hash_includes_untracked_file_contents(tmp_path: Path) -> None:
    """Untracked inputs are source bytes too, not merely status-path entries."""
    repo = _committed_repo(tmp_path)
    generated = repo / "generated-input.json"

    generated.write_text('{"variant": 1}\n')
    first = collect_provenance(repo, ["benchmark"], seed=1)
    generated.write_text('{"variant": 2}\n')
    second = collect_provenance(repo, ["benchmark"], seed=1)

    assert first["dirty_patch"] == second["dirty_patch"]
    assert first["dirty_patch"] == "?? generated-input.json"
    assert first["dirty_patch_sha256"] != second["dirty_patch_sha256"]
