"""Smoke tests for the Typer CLI.

The CLI is a thin wrapper that routes through the canonical
``MemoryService``. It MUST NOT write to the repository or indexes
directly; this test exercises the real wiring via a temporary
SQLite database and asserts the on-disk canonical state, plus a
dry-run of the search subcommand.
"""
from __future__ import annotations

import json
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
