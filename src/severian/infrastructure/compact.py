"""Reclaim the pages a purge freed, in the SQLite stores the engine owns.

A purge replaces text with a fixed marker and releases identity keys; the old text is no
longer reachable, but SQLite keeps the pages that held it as free pages until something
rewrites the file. ``wal_checkpoint(TRUNCATE)`` flushes the write-ahead log and truncates
it to zero, and ``VACUUM`` rewrites the database file from live pages only. Both are needed
and both are cheap for these store sizes.

Order matters: the checkpoint has to run first (so the WAL's copy of the old text is gone),
VACUUM's own writes land in the WAL afterwards, and a final checkpoint truncates that.

**Each step can fail on its own, so each step is reported on its own.** A reader holding
the log open does not block the purge (a WAL write proceeds) but it does block the
checkpoint. The result therefore carries the outcome of BOTH checkpoints and of the
VACUUM, plus the reason when one of them did not complete - the caller must never print
"checkpointed and VACUUMed" from a success path (M1 F3).

**What this can and cannot claim.** It compacts the files named here - the canonical store,
the FTS5 index and the vector index. It cannot reach:

* PostgreSQL's dead tuples: an ordinary ``VACUUM`` is not a guarantee of forensic erasure,
  and only ``VACUUM FULL`` rewrites the heap. ``severian purge`` prints the operator command
  and does not run it;
* copies kept outside the store: ``~/.hermes/backups/severian-*``, ``~/backups/severian-*``,
  snapshot files, PostgreSQL base backups, or anything the operator copied elsewhere.

So no "physically purged everywhere" claim is made anywhere in this code or its output.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CompactionResult", "compact_sqlite_stores"]


@dataclass(frozen=True)
class CompactionResult:
    """What one store's checkpoint + VACUUM actually did (bytes are the file size afterwards).

    ``final_checkpointed`` is ``None`` when that step never ran because VACUUM did not
    succeed: a step that did not happen is reported as not attempted, never as a failure
    and never as a success.
    """

    path: str
    bytes: int
    checkpointed: bool
    vacuumed: bool
    final_checkpointed: bool | None
    busy: bool = False
    error: str | None = None

    @property
    def complete(self) -> bool:
        """True only when every step ran and finished: the file is physically compacted."""
        return self.checkpointed and self.vacuumed and self.final_checkpointed is True and self.error is None


def _checkpoint(connection: sqlite3.Connection) -> tuple[bool, bool]:
    """One ``wal_checkpoint(TRUNCATE)``: ``(fully_flushed, busy)``.

    SQLite returns ``(busy, log_frames, checkpointed_frames)``; a non-zero ``busy`` means a
    reader still holds the log, so frames were not all written back and the log was not
    truncated.
    """
    row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if not row:
        return False, True
    busy = int(row[0]) != 0
    return (not busy), busy


def _vacuum(connection: sqlite3.Connection) -> str | None:
    """``VACUUM`` the store; the reason it failed, or ``None`` when it ran."""
    try:
        connection.execute("VACUUM")
    except sqlite3.Error as exc:  # a busy store, a read-only file, a corrupt page ...
        return f"VACUUM failed: {exc}"
    return None


def _compact(path: Path) -> CompactionResult:
    """Checkpoint, VACUUM and checkpoint again, on a dedicated autocommit connection."""
    try:
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    except sqlite3.Error as exc:
        return CompactionResult(
            path=str(path),
            bytes=path.stat().st_size if path.exists() else 0,
            checkpointed=False,
            vacuumed=False,
            final_checkpointed=None,
            busy=False,
            error=f"could not open the store: {exc}",
        )
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        checkpointed, busy = _checkpoint(connection)
        vacuum_error = _vacuum(connection)
        vacuumed = vacuum_error is None
        final_checkpointed: bool | None = None
        error = vacuum_error
        if vacuumed:
            final_checkpointed, final_busy = _checkpoint(connection)
            busy = busy or final_busy
            if not final_checkpointed:
                error = "the checkpoint after VACUUM could not flush the WAL (the store is in use)"
        if not checkpointed and error is None:
            error = "the checkpoint could not flush the WAL (the store is in use)"
        elif not checkpointed:
            error = f"{error}; the first checkpoint also found the store in use"
        size = path.stat().st_size
    finally:
        connection.close()
    return CompactionResult(
        path=str(path),
        bytes=size,
        checkpointed=checkpointed,
        vacuumed=vacuumed,
        final_checkpointed=final_checkpointed,
        busy=busy,
        error=error,
    )


def compact_sqlite_stores(paths: Iterable[str | Path]) -> list[CompactionResult]:
    """Compact each existing SQLite store once, in the order given.

    Never raises for a store it can open: a busy or failing step is reported in that
    store's result so the caller can tell the operator exactly which file is not clean.
    Missing paths are skipped (a bundle built on an in-memory store has no files). The
    caller decides *when* to compact: after a confirmed purge, never on a read path.
    """
    results: list[CompactionResult] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw)
        if path in seen or not path.exists() or not path.is_file():
            continue
        seen.add(path)
        results.append(_compact(path))
    return results
