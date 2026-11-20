"""M1 correction (F3) - a compaction reports what it did, never more.

A purge replaces text with a fixed marker; the pages that held the old text are free
pages until something rewrites the store file. ``compact_sqlite_stores`` checkpoints
the write-ahead log, VACUUMs the file and checkpoints again. Each of those three steps
can fail independently on a store that is in use, and the caller has to say which one
did - an unconditional "checkpointed and VACUUMed" sentence is a lie whenever a reader
holds the log open.

These tests are unit-level: a real store for the happy path, and injected step results
for each failure mode, so no long lock wait is needed. ``tests/cli/test_cli_purge.py``
holds the one real busy-reader control.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import severian.infrastructure.compact as compact_module
from severian.infrastructure.compact import compact_sqlite_stores


def _store(path: Path) -> Path:
    """A small WAL-mode store with one row, so a checkpoint has something to flush."""
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO t (body) VALUES ('old text')")
    finally:
        connection.close()
    return path


def test_an_idle_store_reports_both_checkpoints_and_the_vacuum(tmp_path: Path) -> None:
    path = _store(tmp_path / "idle.db")

    [result] = compact_sqlite_stores([path])

    assert result.checkpointed is True
    assert result.vacuumed is True
    assert result.final_checkpointed is True
    assert result.busy is False
    assert result.error is None
    assert result.complete is True
    assert result.path == str(path)


def test_a_busy_checkpoint_is_reported_and_never_claimed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Probe D's state: the log cannot be flushed while a reader holds it open."""
    path = _store(tmp_path / "busy.db")
    monkeypatch.setattr(compact_module, "_checkpoint", lambda connection: (False, True))

    [result] = compact_sqlite_stores([path])

    assert result.checkpointed is False
    assert result.busy is True
    assert result.complete is False
    assert result.error is not None and "in use" in result.error.lower()


def test_a_failed_final_checkpoint_is_reported_as_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """VACUUM's own writes land in the log; a busy final checkpoint leaves them there."""
    path = _store(tmp_path / "final.db")
    steps = iter([(True, False), (False, True)])
    monkeypatch.setattr(compact_module, "_checkpoint", lambda connection: next(steps))

    [result] = compact_sqlite_stores([path])

    assert result.checkpointed is True
    assert result.vacuumed is True
    assert result.final_checkpointed is False
    assert result.complete is False
    assert result.error is not None and "after VACUUM" in result.error


def test_a_failing_vacuum_is_reported_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _store(tmp_path / "vacuum.db")
    monkeypatch.setattr(compact_module, "_vacuum", lambda connection: "VACUUM failed: database is locked")

    [result] = compact_sqlite_stores([path])

    assert result.vacuumed is False
    assert result.final_checkpointed is None, "a step that never ran must not be reported as failed"
    assert result.complete is False
    assert result.error == "VACUUM failed: database is locked"


def test_missing_paths_are_skipped_and_every_existing_store_gets_a_result(tmp_path: Path) -> None:
    first = _store(tmp_path / "a.db")
    second = _store(tmp_path / "b.db")
    missing = tmp_path / "absent.db"

    results = compact_sqlite_stores([first, missing, second, first])

    assert [result.path for result in results] == [str(first), str(second)]
