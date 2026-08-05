"""Smoke tests for the Typer CLI.

The CLI is a thin wrapper that routes through the canonical
``MemoryService``. It MUST NOT write to the repository or indexes
directly; this test exercises the real wiring via a temporary
SQLite database and asserts the on-disk canonical state, plus a
dry-run of the search subcommand.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from severian.cli.app import cli


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


def test_ingest_command_writes_canonical_record_and_enqueues_job(data_root: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "ingest",
            "--database",
            str(data_root / "severian.db"),
            "--fts",
            str(data_root / "fts.db"),
            "--vectors",
            str(data_root / "vectors.db"),
            "--tenant",
            "t1",
            "--profile",
            "p1",
            "--collection",
            "c1",
            "--agent",
            "a1",
            "--session",
            "s1",
            "--content",
            "  Hello\r\nworld  ",
            "--source",
            json.dumps({"uri": "note://1"}),
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output.strip().splitlines()[-1])
    assert body["content"] == "Hello\nworld"
    assert body["job_kind"] == "derive"

    # The canonical observation must be on disk afterwards.
    from severian.domain.models import Scope
    from severian.infrastructure.sqlite import SQLiteRepository
    repo = SQLiteRepository(data_root / "severian.db")
    repo.initialize()
    scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    observations = repo.list_observations(scope)
    assert len(observations) == 1
    assert observations[0].content == "Hello\nworld"
    jobs = [job for job in repo.pending_outbox()]
    assert any(ev.scope == scope and ev.kind.startswith("observation.") for ev in jobs)
    repo.close()


def test_ingest_rejects_empty_content(data_root: Path):
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "ingest",
            "--database",
            str(data_root / "severian.db"),
            "--tenant",
            "t1",
            "--profile",
            "p1",
            "--collection",
            "c1",
            "--agent",
            "a1",
            "--session",
            "s1",
            "--content",
            "  \n\t  ",
            "--source",
            "{}",
        ],
    )
    assert result.exit_code != 0


def test_search_command_routes_through_service(data_root: Path):
    runner = CliRunner()
    db = data_root / "severian.db"
    fts = data_root / "fts.db"
    vectors = data_root / "vectors.db"

    # Seed
    seed_result = runner.invoke(
        cli,
        [
            "ingest",
            "--database", str(db),
            "--fts", str(fts),
            "--vectors", str(vectors),
            "--tenant", "t1", "--profile", "p1", "--collection", "c1",
            "--agent", "a1", "--session", "s1",
            "--content", "Ada founded a company",
            "--source", "{}",
        ],
    )
    assert seed_result.exit_code == 0, seed_result.output

    # Search with --derive True. CLI runs the default FakeGateway by
    # default; for hermetic tests we keep the local hash embedding.
    result = runner.invoke(
        cli,
        [
            "search",
            "--database", str(db),
            "--fts", str(fts),
            "--vectors", str(vectors),
            "--tenant", "t1", "--profile", "p1", "--collection", "c1",
            "--agent", "a1", "--session", "s1",
            "--query", "Ada",
            "--derive",
        ],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.output.strip().splitlines()[-1])
    assert body["hits"]
    assert all(h["scope"]["profile_id"] == "p1" for h in body["hits"])


def test_cli_is_installed_as_module():
    """``python -m severian.cli --help`` must succeed."""
    result = subprocess.run(
        [sys.executable, "-m", "severian.cli", "--help"],
        capture_output=True,
        text=True,
        cwd="/home/kensei/repos/Severian",
    )
    assert result.returncode == 0, result.stderr
    assert "ingest" in result.stdout
    assert "search" in result.stdout


def test_export_import_roundtrip_restores_searchable_store(data_root: Path):
    """Export a snapshot, import into a fresh store, and search the result.

    Regression for the release artifact: a restore must leave the store
    immediately searchable (projections rebuilt for imported scopes), and
    a tampered snapshot must be rejected.
    """
    runner = CliRunner()
    db = data_root / "severian.db"
    fts = data_root / "fts.db"
    vectors = data_root / "vectors.db"
    snapshot = data_root / "snapshot.json"
    restored = data_root / "restored.db"
    restored_fts = data_root / "restored-fts.db"
    restored_vectors = data_root / "restored-vectors.db"

    scope_flags = ["--tenant", "t1", "--profile", "p1", "--collection", "c1", "--agent", "a1", "--session", "s1"]

    # Seed and promote.
    assert runner.invoke(
        cli, ["ingest", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), *scope_flags,
              "--content", "Ada founded a company", "--source", "{}"]
    ).exit_code == 0
    assert runner.invoke(
        cli, ["work", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), "--max-jobs", "10"]
    ).exit_code == 0

    # Export.
    export_result = runner.invoke(
        cli, ["export", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), "--output", str(snapshot)]
    )
    assert export_result.exit_code == 0, export_result.output
    assert snapshot.exists()

    # Import into a fresh store; default rebuild makes it searchable.
    import_result = runner.invoke(
        cli, ["import", "--database", str(restored), "--fts", str(restored_fts), "--vectors", str(restored_vectors),
              "--input", str(snapshot)]
    )
    assert import_result.exit_code == 0, import_result.output
    import_body = json.loads(import_result.output.strip().splitlines()[-1])
    assert import_body["status"] == "imported"
    assert len(import_body["rebuilt_scopes"]) == 1

    search_result = runner.invoke(
        cli, ["search", "--database", str(restored), "--fts", str(restored_fts), "--vectors", str(restored_vectors),
              *scope_flags, "--query", "Ada"]
    )
    assert search_result.exit_code == 0, search_result.output
    search_body = json.loads(search_result.output.strip().splitlines()[-1])
    assert search_body["hits"], "restored store must be searchable after import"


def test_import_rejects_tampered_snapshot(data_root: Path):
    """A checksum-violating snapshot must abort the import."""
    runner = CliRunner()
    bad = data_root / "bad.json"
    bad.write_text(
        json.dumps({"format": "severian-v1", "schema_version": 1, "records": [], "checksum": "deadbeef"}),
        encoding="utf-8",
    )
    result = runner.invoke(
        cli,
        [
            "import",
            "--database", str(data_root / "bad.db"),
            "--fts", str(data_root / "bad-fts.db"),
            "--vectors", str(data_root / "bad-vectors.db"),
            "--input", str(bad),
        ],
    )
    assert result.exit_code != 0
    assert (
        "checksum" in result.output.lower()
        or "tamper" in result.output.lower()
        or "valueerror" in result.output.lower()
        or "cannot import" in result.output.lower()
    )


def test_export_refuses_symlink_destination(data_root: Path):
    """P2: export must not write through a symlink (victim-file overwrite)."""
    runner = CliRunner()
    db = data_root / "severian.db"
    fts = data_root / "fts.db"
    vectors = data_root / "vectors.db"
    victim = data_root / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    link = data_root / "snapshot.json"
    link.symlink_to(victim)

    result = runner.invoke(
        cli, ["export", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), "--output", str(link)]
    )
    assert result.exit_code != 0
    assert result.exception is not None
    assert "symlink" in str(result.exception).lower()
    assert victim.read_text(encoding="utf-8") == "keep me"  # victim untouched
    # No stray temp files left behind.
    leftovers = [p.name for p in data_root.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_export_import_records_admin_audit(data_root: Path):
    """SEC-02: export and import must be recorded in the audit trail
    under the reserved system scope."""
    runner = CliRunner()
    db = data_root / "severian.db"
    fts = data_root / "fts.db"
    vectors = data_root / "vectors.db"
    snapshot = data_root / "snapshot.json"

    scope_flags = ["--tenant", "t1", "--profile", "p1", "--collection", "c1", "--agent", "a1", "--session", "s1"]
    assert runner.invoke(
        cli, ["ingest", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), *scope_flags,
              "--content", "Ada founded a company", "--source", "{}"]
    ).exit_code == 0

    export_result = runner.invoke(
        cli, ["export", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), "--output", str(snapshot)]
    )
    assert export_result.exit_code == 0, export_result.output
    assert snapshot.exists()

    import_result = runner.invoke(
        cli, ["import", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), "--input", str(snapshot)]
    )
    assert import_result.exit_code == 0, import_result.output

    # Inspect the audit trail under the system scope directly.
    from severian.domain.models import Scope
    from severian.infrastructure.sqlite import SQLiteRepository

    repo = SQLiteRepository(db)
    repo.initialize()
    actions = [a.action for a in repo.list_audit(
        Scope(tenant_id="_system", profile_id="_", collection_id="_", agent_id="_", session_id="_")
    )]
    repo.close()
    assert "store.export" in actions
    assert "store.import" in actions


def test_import_rebuild_failure_reports_stale_projections(data_root: Path):
    """DATA-02: if projection rebuild fails after a canonical import, the
    canonical records are committed (atomic restore) but the CLI must
    explicitly report the stale projections so the operator can re-run
    with --rebuild."""
    runner = CliRunner()
    db = data_root / "severian.db"
    fts = data_root / "fts.db"
    vectors = data_root / "vectors.db"
    snapshot = data_root / "snapshot.json"

    scope_flags = ["--tenant", "t1", "--profile", "p1", "--collection", "c1", "--agent", "a1", "--session", "s1"]
    assert runner.invoke(
        cli, ["ingest", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), *scope_flags,
              "--content", "Ada founded a company", "--source", "{}"]
    ).exit_code == 0
    # Derive the memory (a memory record is required for rebuild scopes).
    assert runner.invoke(
        cli, ["work", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), "--max-jobs", "10"]
    ).exit_code == 0
    assert runner.invoke(
        cli, ["export", "--database", str(db), "--fts", str(fts), "--vectors", str(vectors), "--output", str(snapshot)]
    ).exit_code == 0

    # Poison the VECTOR path (valid DB but read-only) so rebuild's write
    # fails at rebuild time, not at bundle construction.
    vec_path = data_root / "vectors.db"
    os.chmod(vec_path, 0o400)

    # Import into a FRESH canonical store so records are actually new and
    # the rebuild runs (importing into the same store is idempotent-skip).
    fresh_db = data_root / "fresh.db"
    result = runner.invoke(
        cli, ["import", "--database", str(fresh_db), "--fts", str(fts), "--vectors", str(vec_path),
              "--input", str(snapshot)]
    )
    os.chmod(vec_path, 0o600)
    # Import itself succeeds (canonical committed atomically).
    assert result.exit_code == 0, result.output
    # The stale-projection warning must be visible.
    combined = result.output + (result.stderr or "")
    assert "projections are stale" in combined or "WARNING" in combined
