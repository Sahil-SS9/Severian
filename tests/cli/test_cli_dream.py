"""CLI surface of the dreaming producer: the switch, the dry run, ``--confirm`` and the tick."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from severian.cli.app import cli
from severian.composition import build_bundle
from severian.domain.models import Collection, Memory, Observation, Scope

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _plain(text: str) -> str:
    """CLI output without colour codes or line wrapping (CI forces colour; rich splits option names)."""
    return " ".join(_ANSI.sub("", text).replace("│", " ").split())


SCOPE_FLAGS = ["--tenant", "t1", "--profile", "p1", "--collection", "c1", "--agent", "a1", "--session", "s1"]


@pytest.fixture
def store(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A store with one near-duplicate pair, written through the real write path."""
    bundle = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    bundle.repository.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    for content in ("the user keeps a steel kettle", "the user keeps a steel kettle"):
        observation = bundle.repository.add_observation(
            Observation(scope=scope, content=content, raw_content=content, source={"uri": f"note://{uuid.uuid4().hex}"})
        )
        bundle.repository.add_memory(
            Memory(
                scope=scope,
                kind="fact",
                content=content,
                source_observation_ids=[observation.id],
                model_id="test",
                prompt_version="p1",
                confidence=0.9,
            )
        )
    bundle.close()
    return tmp_path / "s.db", tmp_path / "s.fts", tmp_path / "s.vec"


def invoke(store: tuple[Path, Path, Path], *args: str) -> Result:
    database, fts, vectors = store
    return CliRunner().invoke(
        cli,
        [
            "dream",
            "--database",
            str(database),
            "--fts",
            str(fts),
            "--vectors",
            str(vectors),
            *SCOPE_FLAGS,
            *args,
        ],
    )


def test_the_command_refuses_while_the_switch_is_off(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_DREAMING", raising=False)
    result = invoke(store, "--mode", "consolidate")
    assert result.exit_code != 0
    assert "SEVERIAN_DREAMING" in result.output


def test_a_dry_run_prints_the_plan_and_writes_nothing(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    result = invoke(store, "--mode", "consolidate")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["confirmed"] is False
    assert payload["duplicate_groups"] == 1
    assert payload["planned"] and payload["planned"][0]["supersede"]
    assert payload["consolidated_memories"] == 0
    # Nothing was written: a second dry run reports the identical plan.
    again = json.loads(invoke(store, "--mode", "consolidate").output)
    assert again["planned"] == payload["planned"]


def test_confirm_applies_and_the_run_converges(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    applied = json.loads(invoke(store, "--mode", "consolidate", "--confirm").output)
    assert applied["confirmed"] is True
    assert applied["consolidated_memories"] == 1
    second = json.loads(invoke(store, "--mode", "consolidate", "--confirm").output)
    assert second["duplicate_groups"] == 0
    assert second["consolidated_memories"] == 0


def test_a_bad_mode_and_bad_policy_are_refused(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    assert invoke(store, "--mode", "dream-a-little").exit_code != 0
    assert invoke(store, "--mode", "retention", "--policy", "{not json").exit_code != 0


def test_the_retention_archive_dry_run_archives_nothing(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    result = invoke(store, "--mode", "retention-archive", "--policy", '{"max_age_days": 0.0000001}')
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["confirmed"] is False
    assert payload["archived_candidates"], "the dry run must show the candidates it would archive"
    # Nothing was archived: the second dry run sees the same candidates (a confirmed run empties them).
    again = json.loads(invoke(store, "--mode", "retention-archive", "--policy", '{"max_age_days": 0.0000001}').output)
    assert again["archived_candidates"] == payload["archived_candidates"]


def test_the_work_tick_requires_both_scope_flags(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    database, fts, vectors = store
    result = CliRunner().invoke(
        cli,
        ["work", "--database", str(database), "--fts", str(fts), "--vectors", str(vectors), "--dream-tenant", "t1"],
    )
    assert result.exit_code != 0
    assert "--dream-profile" in _plain(result.output)


def test_the_work_tick_is_off_without_the_switch(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_DREAMING", raising=False)
    database, fts, vectors = store
    result = CliRunner().invoke(
        cli,
        [
            "work",
            "--database",
            str(database),
            "--fts",
            str(fts),
            "--vectors",
            str(vectors),
            "--dream-tenant",
            "t1",
            "--dream-profile",
            "p1",
        ],
    )
    assert result.exit_code != 0
    assert "SEVERIAN_DREAMING" in result.output


def test_the_work_tick_queues_and_runs(store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_DREAMING", "1")
    database, fts, vectors = store
    result = CliRunner().invoke(
        cli,
        [
            "work",
            "--database",
            str(database),
            "--fts",
            str(fts),
            "--vectors",
            str(vectors),
            "--dream-tenant",
            "t1",
            "--dream-profile",
            "p1",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dream_scopes"] == 1
    assert payload["dream_processed"] == len(payload["dream_jobs"]) == 3
    assert payload["dream_failed"] == 0
