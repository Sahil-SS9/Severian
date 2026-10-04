"""``severian audit``: read-only store check and safe repair (Step 5, next-improvements plan)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

from severian.application.worker import DerivationWorker
from severian.cli.app import cli
from severian.composition import build_bundle
from severian.domain.models import Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="octacon", collection_id="profile", agent_id="octacon", session_id="shared"
)
FILES = ("s.db", "s.fts", "s.vec")


@contextmanager
def _edit(path: Path):
    """Commit and CLOSE, so the log is folded into the file and the next hash is of settled bytes."""
    with closing(sqlite3.connect(path)) as connection:
        yield connection
        connection.commit()


def _fingerprint(folder: Path) -> dict[str, str]:
    return {name: hashlib.sha256((folder / name).read_bytes()).hexdigest() for name in FILES}


def _flags(folder: Path) -> list[str]:
    return ["--database", str(folder / "s.db"), "--fts", str(folder / "s.fts"), "--vectors", str(folder / "s.vec")]


def _run(folder: Path, *extra: str) -> tuple[int, dict]:
    result = CliRunner().invoke(cli, ["audit", *_flags(folder), *extra])
    return result.exit_code, json.loads(result.output.strip().splitlines()[-1])


@pytest.fixture
def healthy(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("SEVERIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("SEVERIAN_ANTICIPATION", "1")
    folder = tmp_path / "store"
    folder.mkdir()
    bundle = build_bundle(database=folder / "s.db", fts=folder / "s.fts", vectors=folder / "s.vec")
    bundle.derivation._cue_generator = lambda text: ["which beverage does Alice prefer"] if "Alice" in text else []
    for text in ("Alice likes green tea", "Bob watched a football match", "Whenever you use git, check the branch"):
        bundle.service.ingest_observation(SCOPE, content=text, source={"session_id": "c"})
        assert (
            DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
            .drain(gateway=bundle.gateway)
            .failed
            == 0
        )
    bundle.close()
    return folder


def damaged(healthy: Path, tmp_path: Path) -> Path:
    """A copy of the healthy store with one fault of each kind."""
    folder = tmp_path / "damaged"
    shutil.copytree(healthy, folder)
    with _edit(folder / "s.fts") as fts:
        row = fts.execute("SELECT memory_id FROM memory_fts WHERE content LIKE 'Bob%'").fetchone()
        fts.execute("DELETE FROM memory_fts WHERE memory_id = ?", (row[0],))  # missing lexical row
        fts.execute("DELETE FROM cue_fts")  # missing cue row
        fts.execute(
            "INSERT INTO memory_fts (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, "
            "content, kind, source_ids) VALUES "
            "('ghost', 'kensei', 'octacon', 'profile', 'octacon', 'shared', 'ghost text', 'fact', '')"
        )  # orphan lexical row
    with _edit(folder / "s.vec") as vec:
        row = vec.execute("SELECT memory_id FROM memory_vectors WHERE content LIKE 'Whenever%'").fetchone()
        vec.execute("DELETE FROM memory_vectors WHERE memory_id = ?", (row[0],))  # missing vector row
    with _edit(folder / "s.db") as store:
        job_id = store.execute("SELECT id FROM records WHERE record_type = 'job' LIMIT 1").fetchone()[0]
        store.execute(
            "UPDATE records SET status = 'running', lease_until = '2020-01-01T00:00:00+00:00' WHERE id = ?", (job_id,)
        )  # stuck job
    return folder


def test_a_healthy_store_has_no_findings_and_exit_zero(healthy):
    code, report = _run(healthy)
    assert code == 0 and report["ok"] is True and report["findings"] == []
    assert report["mode"] == "read-only"
    (counts,) = report["scopes"].values()
    assert counts["memories"] == counts["lexical"] == counts["vector"] == 3
    assert counts["cues_expected"] == counts["cues_indexed"] == 1


def test_audit_reports_every_planted_fault(healthy, tmp_path):
    folder = damaged(healthy, tmp_path)
    code, report = _run(folder)
    kinds = report["findings_by_kind"]
    assert code == 1 and report["ok"] is False
    for kind in ("missing_lexical_row", "orphan_lexical_row", "missing_vector_row", "missing_cue_row", "stuck_job"):
        assert kinds.get(kind) == 1, (kind, kinds)
    assert report["repairable_count"] == 4  # everything except the orphan row
    assert report["jobs"]["stuck_leases"] == 1


def test_read_only_audit_changes_not_one_byte(healthy, tmp_path):
    folder = damaged(healthy, tmp_path)
    before = _fingerprint(folder)
    _run(folder)
    _run(folder)
    assert _fingerprint(folder) == before


def test_read_only_audit_never_creates_the_cue_table_on_an_older_store(healthy, tmp_path):
    folder = tmp_path / "old"
    shutil.copytree(healthy, folder)
    with _edit(folder / "s.fts") as fts:
        fts.execute("DROP TABLE cue_fts")
    before = _fingerprint(folder)
    code, report = _run(folder)
    assert _fingerprint(folder) == before
    with _edit(folder / "s.fts") as fts:
        assert fts.execute("SELECT 1 FROM sqlite_master WHERE name = 'cue_fts'").fetchone() is None
    assert report["findings_by_kind"] == {"missing_cue_row": 1} and code == 1


def test_repair_fixes_only_the_safe_things_and_deletes_nothing(healthy, tmp_path):
    folder = damaged(healthy, tmp_path)
    with _edit(folder / "s.db") as store:
        records_before = store.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    code, report = _run(folder, "--repair")
    assert report["mode"] == "repair"
    assert report["repair"]["requeued_jobs"] == 1
    assert report["repair"]["lexical_rows"] >= 1 and report["repair"]["vector_rows"] >= 1
    assert report["repair"]["errors"] == []
    # only the orphan row is left, and it is reported, not deleted
    assert report["findings_by_kind"] == {"orphan_lexical_row": 1} and code == 1
    with _edit(folder / "s.fts") as fts:
        assert fts.execute("SELECT COUNT(*) FROM memory_fts WHERE memory_id = 'ghost'").fetchone()[0] == 1
        assert fts.execute("SELECT COUNT(*) FROM cue_fts").fetchone()[0] == 1
    with _edit(folder / "s.db") as store:
        assert store.execute("SELECT COUNT(*) FROM records").fetchone()[0] >= records_before - 0
        assert store.execute("SELECT COUNT(*) FROM records WHERE record_type = 'memory'").fetchone()[0] == 3
        assert store.execute("SELECT COUNT(*) FROM records WHERE record_type = 'observation'").fetchone()[0] == 3


def test_repair_on_a_healthy_store_is_a_no_op(healthy):
    code, report = _run(healthy, "--repair")
    assert code == 0 and report["repair"] == {
        "requeued_jobs": 0,
        "lexical_rows": 0,
        "vector_rows": 0,
        "cue_rows": 0,
        "errors": [],
    }


def test_memory_and_relation_that_lost_their_source_are_reported(healthy, tmp_path):
    folder = tmp_path / "sources"
    shutil.copytree(healthy, folder)
    with _edit(folder / "s.db") as store:
        store.execute(
            "UPDATE records SET payload = json_set(payload, '$.superseded_by_id', 'someone-else') "
            "WHERE record_type = 'observation'"
        )
    code, report = _run(folder)
    assert report["findings_by_kind"]["memory_without_source"] == 1
    assert code == 1


def test_broken_and_looping_replacement_chains_are_reported(healthy, tmp_path):
    folder = tmp_path / "chains"
    shutil.copytree(healthy, folder)
    with _edit(folder / "s.db") as store:
        ids = [r[0] for r in store.execute("SELECT id FROM records WHERE record_type = 'memory' ORDER BY id")]
        store.execute(
            "UPDATE records SET payload = json_set(payload, '$.superseded_by_id', 'nowhere') WHERE id = ?", (ids[0],)
        )
        store.execute(
            "UPDATE records SET payload = json_set(payload, '$.superseded_by_id', ?) WHERE id = ?", (ids[2], ids[1])
        )
        store.execute(
            "UPDATE records SET payload = json_set(payload, '$.superseded_by_id', ?) WHERE id = ?", (ids[1], ids[2])
        )
    _, report = _run(folder)
    kinds = report["findings_by_kind"]
    assert kinds["replacement_target_missing"] == 1
    assert kinds["replacement_loop"] == 1


def test_a_missing_store_file_is_a_clean_error(tmp_path):
    result = CliRunner().invoke(
        cli,
        ["audit", "--database", str(tmp_path / "a"), "--fts", str(tmp_path / "b"), "--vectors", str(tmp_path / "c")],
    )
    assert result.exit_code != 0 and "not found" in result.output
