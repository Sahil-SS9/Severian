"""Unit tests for the approved-path policy (SEC-04 / E-2)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from severian.cli.path_policy import (
    PathPolicyError,
    check_read_source,
    check_write_destination,
    resolve_approved,
)


def test_write_destination_allows_regular_path(tmp_path: Path) -> None:
    target = tmp_path / "snapshot.json"
    resolved = check_write_destination(target, purpose="export")
    assert resolved == target.resolve()


def test_write_destination_rejects_existing_directory(tmp_path: Path) -> None:
    target = tmp_path / "adir"
    target.mkdir()
    with pytest.raises(PathPolicyError):
        check_write_destination(target, purpose="export")


def test_write_destination_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    target = link_dir / "snapshot.json"
    with pytest.raises(PathPolicyError):
        check_write_destination(target, purpose="export")


def test_write_destination_rejects_final_symlink(tmp_path: Path) -> None:
    real_file = tmp_path / "real.json"
    real_file.write_text("{}", encoding="utf-8")
    link = tmp_path / "snapshot.json"
    link.symlink_to(real_file)
    with pytest.raises(PathPolicyError):
        check_write_destination(link, purpose="export")


def test_read_source_allows_regular_file(tmp_path: Path) -> None:
    source = tmp_path / "snapshot.json"
    source.write_text("{}", encoding="utf-8")
    assert check_read_source(source, purpose="import") == source.resolve()


def test_read_source_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PathPolicyError):
        check_read_source(tmp_path / "nope.json", purpose="import")


def test_read_source_rejects_symlink(tmp_path: Path) -> None:
    real_file = tmp_path / "real.json"
    real_file.write_text("{}", encoding="utf-8")
    link = tmp_path / "snapshot.json"
    link.symlink_to(real_file)
    with pytest.raises(PathPolicyError):
        check_read_source(link, purpose="import")


def test_approved_roots_reject_outside(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("SEVERIAN_APPROVED_ROOTS", str(root))
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(PathPolicyError):
        check_read_source(outside, purpose="import")


def test_approved_roots_allow_inside(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("SEVERIAN_APPROVED_ROOTS", str(root))
    inside = root / "snapshot.json"
    inside.write_text("{}", encoding="utf-8")
    assert check_read_source(inside, purpose="import") == inside.resolve()


def test_no_approved_roots_allows_local_paths(tmp_path: Path) -> None:
    # When SEVERIAN_APPROVED_ROOTS is unset, the operator's explicit
    # path is the trust boundary; only symlink/traversal rules apply.
    os.environ.pop("SEVERIAN_APPROVED_ROOTS", None)
    target = tmp_path / "snapshot.json"
    assert resolve_approved(target, purpose="export") == target.resolve()
