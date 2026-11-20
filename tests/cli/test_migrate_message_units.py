"""S1 migration: dry-run, apply, idempotence and drift report."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from severian.application.worker import DerivationWorker
from severian.cli.app import cli
from severian.composition import build_bundle
from severian.domain.models import Collection, Scope

SCOPE = ["--tenant", "t1", "--profile", "p1", "--collection", "c1", "--agent", "a1", "--session", "s1"]


def _seed(paths: dict[str, Path]) -> None:
    bundle = build_bundle(database=paths["db"], fts=paths["fts"], vectors=paths["vectors"])
    scope = Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")
    bundle.repository.create_collection(Collection(id="c1", tenant_id="t1", name="S1"))
    bundle.service.ingest_observation(
        scope,
        content="[2024/03/15 (Fri)] user: deadline April 1\nassistant: noted",
        source={"turn": 3},
    )
    DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    bundle.close()


def _flags(paths: dict[str, Path]) -> list[str]:
    return [
        "--database",
        str(paths["db"]),
        "--fts",
        str(paths["fts"]),
        "--vectors",
        str(paths["vectors"]),
    ]


def test_migrate_message_units_is_dry_run_then_idempotent(tmp_path: Path) -> None:
    paths = {"db": tmp_path / "m.db", "fts": tmp_path / "m.fts", "vectors": tmp_path / "m.vec"}
    _seed(paths)
    runner = CliRunner()

    dry = runner.invoke(cli, ["migrate", "message-units", *_flags(paths), *SCOPE])
    assert dry.exit_code == 0, dry.output
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["dry_run"] is True
    assert dry_payload["units_planned"] == 2
    assert dry_payload["units_written"] == 0
    assert dry_payload["drift"] == 2

    applied = runner.invoke(cli, ["migrate", "message-units", *_flags(paths), *SCOPE, "--confirm"])
    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["dry_run"] is False
    assert applied_payload["units_written"] == 2
    assert applied_payload["drift"] == 0
    assert applied_payload["backup"] and Path(applied_payload["backup"]).exists()
    assert applied_payload["integrity"] == "ok"
    assert applied_payload["rehearsal"]["units_written"] == 2

    again = runner.invoke(cli, ["migrate", "message-units", *_flags(paths), *SCOPE, "--confirm"])
    assert again.exit_code == 0, again.output
    assert json.loads(again.stdout)["units_written"] == 0
