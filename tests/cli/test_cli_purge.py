"""M1 — ``severian purge``: dry run by default, one explicit ``--confirm`` to act.

The command is a thin operator surface over ``MemoryService``: it lists the records a
purge would scrub (ids and counts only, never content) and does nothing until
``--confirm``. Everything below runs on a synthetic store under ``tmp_path``; the
module redirects ``HOME``/``HERMES_HOME``/``SEVERIAN_STORAGE`` before importing any
adapter so a stray default path can never resolve to a live store.
"""

from __future__ import annotations

# ruff: noqa: E402 — the store guard below MUST run before any adapter is imported.
import json
import os
import sqlite3
import tempfile

_GUARD = tempfile.mkdtemp(prefix="sev-m1-cli-guard-")
os.environ["HOME"] = _GUARD
os.environ["HERMES_HOME"] = os.path.join(_GUARD, "hermes")
os.environ["SEVERIAN_STORAGE"] = os.path.join(_GUARD, "guard.db")

from pathlib import Path

import pytest
from typer.testing import CliRunner

from severian.cli.app import cli
from severian.domain.models import Scope
from severian.infrastructure.sqlite import SQLiteRepository

MARKER = "ZQX-PURGE-MARKER-9F3A"
SECRET = f"The pantry code is {MARKER} for the north shelf"
SCOPE_FLAGS = ["--tenant", "t1", "--profile", "p1", "--collection", "c1", "--agent", "a1", "--session", "s1"]


@pytest.fixture
def stores(tmp_path: Path) -> dict[str, Path]:
    return {
        "db": tmp_path / "severian.db",
        "fts": tmp_path / "severian.fts",
        "vectors": tmp_path / "severian.vec",
    }


def _flags(stores: dict[str, Path]) -> list[str]:
    return ["--database", str(stores["db"]), "--fts", str(stores["fts"]), "--vectors", str(stores["vectors"])]


def _seed(runner: CliRunner, stores: dict[str, Path]) -> str:
    assert (
        runner.invoke(
            cli, ["ingest", *_flags(stores), *SCOPE_FLAGS, "--content", SECRET, "--source", "{}"]
        ).exit_code
        == 0
    )
    assert runner.invoke(cli, ["work", *_flags(stores), "--max-jobs", "10"]).exit_code == 0
    scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    repository = SQLiteRepository(stores["db"])
    try:
        [observation] = repository.list_observations_history(scope)
    finally:
        repository.close()
    return observation.id


def _store_bytes(stores: dict[str, Path]) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for base in stores.values():
        for candidate in (base, Path(str(base) + "-wal"), Path(str(base) + "-shm")):
            if candidate.exists():
                snapshot[str(candidate)] = candidate.read_bytes()
    return snapshot


def _marker_in(files: dict[str, bytes]) -> list[str]:
    needle = MARKER.lower().encode()
    return [path for path, data in files.items() if needle in data.lower()]


def test_purge_is_a_dry_run_by_default_and_writes_nothing(stores: dict[str, Path]) -> None:
    runner = CliRunner()
    observation = _seed(runner, stores)
    before = _store_bytes(stores)

    result = runner.invoke(cli, ["purge", *_flags(stores), *SCOPE_FLAGS, "--observation", observation])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["confirmed"] is False
    assert payload["applied"] is None
    assert observation in payload["plan"]["observation_ids"]
    assert payload["plan"]["counts"]["memory"] == 1
    assert MARKER not in result.stdout and "pantry" not in result.stdout.lower()
    assert _store_bytes(stores) == before, "the dry run must not write a byte"

    still_there = runner.invoke(cli, ["search", *_flags(stores), *SCOPE_FLAGS, "--query", "pantry code north shelf"])
    assert still_there.exit_code == 0
    assert json.loads(still_there.stdout)["hits"], "the dry run must leave the memory findable"


def test_purge_confirm_scrubs_the_store_and_compacts_the_files(stores: dict[str, Path]) -> None:
    runner = CliRunner()
    observation = _seed(runner, stores)
    assert _marker_in(_store_bytes(stores)), "precondition: the marker is on disk before the purge"

    result = runner.invoke(cli, ["purge", *_flags(stores), *SCOPE_FLAGS, "--observation", observation, "--confirm"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["confirmed"] is True
    assert payload["applied"]["memories"] == 1
    assert payload["compacted"], "the confirmed purge must checkpoint and VACUUM the store files"
    assert MARKER not in result.stdout
    assert _marker_in(_store_bytes(stores)) == []

    repository = SQLiteRepository(stores["db"])
    try:
        from severian.domain.models import Scope

        scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
        assert [row.purged for row in repository.list_observations_history(scope)] == [True]
        assert MARKER not in repository.export_json()
    finally:
        repository.close()

    gone = runner.invoke(cli, ["search", *_flags(stores), *SCOPE_FLAGS, "--query", "pantry code north shelf"])
    assert gone.exit_code == 0
    assert json.loads(gone.stdout)["hits"] == []


def test_purge_warnings_state_the_limits_and_never_claim_erasure_everywhere() -> None:
    from severian.cli.app import _purge_warnings

    sqlite_warnings = " ".join(_purge_warnings("sqlite")).lower()
    assert "backup" in sqlite_warnings, "copies outside the store must be called out"
    assert "everywhere" not in sqlite_warnings and "physically purged" not in sqlite_warnings

    pg_warnings = " ".join(_purge_warnings("postgresql")).lower()
    assert "vacuum" in pg_warnings, "the PG dead-tuple caveat belongs in the operator's face"
    assert "not" in pg_warnings and "everywhere" not in pg_warnings


def test_purge_refuses_an_observation_that_is_not_in_the_scope(stores: dict[str, Path]) -> None:
    runner = CliRunner()
    observation = _seed(runner, stores)

    result = runner.invoke(
        cli,
        [
            "purge",
            *_flags(stores),
            "--tenant",
            "t1",
            "--profile",
            "p1",
            "--collection",
            "c1",
            "--agent",
            "a1",
            "--session",
            "a-different-session",
            "--observation",
            observation,
            "--confirm",
        ],
    )

    assert result.exit_code != 0, "a foreign-scope purge must fail, not silently do nothing"
    assert _marker_in(_store_bytes(stores)), "a refused purge must change nothing"


# -- F3 (M1 correction): the compaction claim must match the compaction --------


def test_a_busy_store_reports_the_incomplete_checkpoint_and_still_exits_zero(
    stores: dict[str, Path],
) -> None:
    """Positive control, real lock: a reader holds the log open across the purge.

    The purge itself commits (a reader does not block a write in WAL mode); the
    checkpoint that follows cannot flush the log while that reader is live. The command
    must say so per file, offer the retry, and still exit 0 - the canonical purge DID
    commit, and calling the whole command a failure would be as wrong as claiming the
    files were compacted.
    """
    runner = CliRunner()
    observation = _seed(runner, stores)

    reader = sqlite3.connect(stores["db"], timeout=30, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM records").fetchone()
    try:
        result = runner.invoke(
            cli, ["purge", *_flags(stores), *SCOPE_FLAGS, "--observation", observation, "--confirm"]
        )
    finally:
        reader.execute("ROLLBACK")
        reader.close()

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["confirmed"] is True and payload["applied"]["memories"] == 1
    assert payload["compaction_complete"] is False, "an incomplete checkpoint was reported as complete"
    canonical = [
        entry for entry in payload["compacted"] if entry["path"] == str(stores["db"])
    ]
    assert canonical, "the canonical store was not compacted at all"
    assert canonical[0]["checkpointed"] is False
    assert canonical[0]["error"], "a busy checkpoint must say why it is incomplete"
    warnings = " ".join(payload["warnings"])
    assert str(stores["db"]) in warnings, "the warning must name the file that is not clean"
    assert "retry" in warnings.lower(), "an incomplete compaction must tell the operator what to do"
    assert "were checkpointed and VACUUMed" not in warnings, "the command claimed a compaction it did not do"

    # The logical purge still committed: the row is scrubbed and the text is gone.
    repository = SQLiteRepository(stores["db"])
    try:
        from severian.domain.models import Scope

        scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
        assert [row.purged for row in repository.list_observations_history(scope)] == [True]
        assert MARKER not in repository.export_json()
    finally:
        repository.close()


def test_a_failed_compaction_after_a_committed_purge_is_reported_not_swallowed(
    stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the review named: purge commits, then compaction blows up.

    The operator has to be told both halves - canonical purge applied, physical cleanup
    not done - in one outcome. Exiting non-zero would misreport a committed purge as a
    failed command; saying nothing would hide the pages that are still on disk.
    """
    runner = CliRunner()
    observation = _seed(runner, stores)

    def boom(self: object) -> list[object]:
        raise sqlite3.OperationalError("database is locked")

    from severian.composition import ServiceBundle

    monkeypatch.setattr(ServiceBundle, "compact", boom)

    result = runner.invoke(cli, ["purge", *_flags(stores), *SCOPE_FLAGS, "--observation", observation, "--confirm"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["applied"]["memories"] == 1, "the logical purge is not reported as applied"
    assert payload["compaction_complete"] is False
    assert payload["compacted"] == []
    warnings = " ".join(payload["warnings"])
    assert "database is locked" in warnings, "the compaction error must reach the operator"
    assert "committed" in warnings.lower() and "compact" in warnings.lower()

    repository = SQLiteRepository(stores["db"])
    try:
        from severian.domain.models import Scope

        scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
        assert [row.purged for row in repository.list_observations_history(scope)] == [True]
    finally:
        repository.close()


def test_a_failed_purge_names_itself_once_and_keeps_the_rows(
    stores: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1 R3: the inner refusal is wrapped by the outer handler, so say it once.

    The operator must read ``cannot purge: <reason>``, not
    ``cannot purge: cannot purge: <reason>``. Exit code 2 and the untouched rows are
    part of the contract a failed transaction has to keep.
    """
    runner = CliRunner()
    observation = _seed(runner, stores)

    from severian.application.service import MemoryService

    def boom(self: object, scope: object, observation_id: str) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(MemoryService, "purge_observation", boom)

    result = runner.invoke(
        cli, ["purge", *_flags(stores), *SCOPE_FLAGS, "--observation", observation, "--confirm"]
    )

    assert result.exit_code == 2, result.output
    assert result.output.count("cannot purge") == 1, result.output
    assert "database is locked" in result.output
    assert _marker_in(_store_bytes(stores)), "a failed transaction must leave the rows intact"
