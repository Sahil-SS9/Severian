"""Truth state (Step 1, next-improvements plan): current / historical / transition / concurrent.

Fixes the fault where "open Mondays" then "open Fridays" hid Monday: wording similarity alone
no longer hides anything. An older statement is hidden only when a newer one certainly replaces
it (change wording, or one-off mutually exclusive values). Nothing is ever deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from severian.application.statements import assess_truth, has_change_wording, replaces
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Relation, Scope, TruthState

SCOPE = Scope(
    tenant_id="kensei", profile_id="octacon", collection_id="profile", agent_id="octacon", session_id="shared"
)


@pytest.fixture(params=["on", "off"])
def bundle(request, tmp_path: Path, monkeypatch):
    # "on" = truth state enabled via the environment setting; "off" = legacy rule.
    if request.param == "on":
        monkeypatch.setenv("SEVERIAN_TRUTH_STATE", "1")
    else:
        monkeypatch.delenv("SEVERIAN_TRUTH_STATE", raising=False)
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    built.param = request.param  # type: ignore[attr-defined]
    yield built
    built.close()


def say(bundle: ServiceBundle, *statements: str) -> None:
    for statement in statements:
        bundle.service.ingest_observation(SCOPE, content=statement, source={"session_id": "chat-1"})
        worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
        summary = worker.drain(gateway=bundle.gateway)
        assert summary.failed == 0


def recalled(bundle: ServiceBundle, query: str, view: str = "now", limit: int = 20):
    return bundle.service.search(SCOPE, query=query, limit=limit, view=view)


# -- pure rules ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("older", "newer", "expected"),
    [
        ("The shop is open Mondays", "The shop is open Fridays", False),
        ("The shop is open Mondays", "The shop is now open Fridays", True),
        ("The shop is open Mondays", "The shop is no longer open Mondays", True),
        ("The meeting is on Tuesday", "The meeting is on Thursday", True),
        ("The meeting is on Tuesday", "The meeting is now on Thursday", True),
        ("The deploy window is Thursday afternoon", "The deploy window is Friday morning", True),
        ("The clinic is open every Monday", "The clinic is open every Friday", False),
        ("The standup is at 09:15", "The standup is at 09:30", True),
        ("The user prefers dark mode", "The user prefers green tea", False),
    ],
)
def test_replaces_only_when_certain(older, newer, expected):
    assert replaces(older, newer) is expected


@pytest.mark.parametrize(
    "text", ["It is now on Thursday", "She moved to Leeds", "Open Fridays instead", "no longer open"]
)
def test_change_wording_detected(text):
    assert has_change_wording(text)


def test_change_wording_absent_in_plain_statements():
    assert not has_change_wording("The shop is open Mondays")


def test_assess_truth_labels_each_state():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        ("a", "The meeting is on Tuesday", base),
        ("b", "The meeting is now on Thursday", base + timedelta(days=1)),
        ("c", "The shop is open Mondays", base),
        ("d", "The shop is open Fridays", base + timedelta(days=1)),
        ("e", "Alice likes green tea", base),
    ]
    states = assess_truth(rows, content=lambda r: r[1], created_at=lambda r: r[2], key=lambda r: r[0])
    assert states["a"].state is TruthState.HISTORICAL and states["a"].replaced_by == "b"
    assert states["b"].state is TruthState.TRANSITION and states["b"].previous == ("a",)
    assert states["c"].state is TruthState.CONCURRENT and states["d"].state is TruthState.CONCURRENT
    assert states["e"].state is TruthState.CURRENT


def test_chain_marks_every_old_value_historical_and_newest_replacer():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    rows = [
        ("1", "The meeting is on Tuesday", base),
        ("2", "The meeting is on Wednesday", base + timedelta(days=1)),
        ("3", "The meeting is on Thursday", base + timedelta(days=2)),
    ]
    states = assess_truth(rows, content=lambda r: r[1], created_at=lambda r: r[2], key=lambda r: r[0])
    assert states["1"].state is TruthState.HISTORICAL and states["1"].replaced_by == "3"
    assert states["2"].state is TruthState.HISTORICAL and states["2"].replaced_by == "3"
    assert states["3"].state is TruthState.TRANSITION


def test_relation_truth_state_follows_validity():
    relation = Relation(scope=SCOPE, subject_id="a", predicate="works_at", object_id="b")
    assert relation.truth_state is TruthState.CURRENT
    ended = relation.replaced_by(
        Relation(scope=SCOPE, subject_id="a", predicate="works_at", object_id="c"), at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert ended is not None and ended.truth_state is TruthState.HISTORICAL


# -- service ------------------------------------------------------------------


def test_monday_then_friday_are_both_returned(bundle):
    """The live fault: ``open Fridays`` must not hide ``open Mondays``."""
    say(bundle, "The shop is open Mondays", "The shop is open Fridays")
    hits = [hit.content for hit in recalled(bundle, "shop open")]
    if bundle.param == "on":
        assert "The shop is open Mondays" in hits and "The shop is open Fridays" in hits
    else:
        assert hits == ["The shop is open Fridays"]  # legacy rule is unchanged while the setting is off


def test_moved_returns_the_newer_value(bundle):
    say(bundle, "The meeting is on Tuesday", "The meeting is now on Thursday")
    hits = [hit.content for hit in recalled(bundle, "meeting")]
    assert "The meeting is now on Thursday" in hits
    assert "The meeting is on Tuesday" not in hits


def test_history_view_shows_both_with_labels(bundle):
    say(bundle, "The meeting is on Tuesday", "The meeting is now on Thursday")
    hits = {hit.content: hit for hit in recalled(bundle, "meeting", view="history")}
    assert hits["The meeting is on Tuesday"].truth_state == "historical"
    assert hits["The meeting is on Tuesday"].replaced_by_id == hits["The meeting is now on Thursday"].memory_id
    assert hits["The meeting is now on Thursday"].truth_state == "transition"


def test_changes_view_returns_the_change_with_what_it_replaced(bundle):
    say(bundle, "The meeting is on Tuesday", "The meeting is now on Thursday", "Alice likes green tea")
    hits = recalled(bundle, "meeting tea", view="changes")
    assert [hit.content for hit in hits] == ["The meeting is now on Thursday"]
    assert hits[0].truth_state == "transition"
    assert hits[0].previous == ("The meeting is on Tuesday",)


def test_old_fact_is_never_deleted(bundle):
    say(bundle, "The meeting is on Tuesday", "The meeting is now on Thursday")
    history = [memory.content for memory in bundle.repository.list_memories_history(SCOPE)]
    assert "The meeting is on Tuesday" in history


def test_unknown_view_is_rejected(bundle):
    with pytest.raises(ValueError, match="view must be one of"):
        recalled(bundle, "meeting", view="future")


# -- plugin and CLI -----------------------------------------------------------


def test_cli_search_view_history_labels_historical(tmp_path: Path):
    import json

    from typer.testing import CliRunner

    from severian.cli.app import cli

    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    try:
        say(built, "The meeting is on Tuesday", "The meeting is now on Thursday")
    finally:
        built.close()
    scope_flags = [
        "--tenant", "kensei", "--profile", "octacon", "--collection", "profile", "--agent", "octacon",
        "--session", "shared",
    ]
    paths = [
        "--database", str(tmp_path / "s.db"), "--fts", str(tmp_path / "s.fts"), "--vectors", str(tmp_path / "s.vec"),
    ]
    result = CliRunner().invoke(cli, ["search", *paths, *scope_flags, "--query", "meeting", "--view", "history"])
    assert result.exit_code == 0, result.output
    states = {hit["content"]: hit["truth_state"] for hit in json.loads(result.output)["hits"]}
    assert states == {"The meeting is on Tuesday": "historical", "The meeting is now on Thursday": "transition"}
    bad = CliRunner().invoke(cli, ["search", *paths, *scope_flags, "--query", "meeting", "--view", "future"])
    assert bad.exit_code != 0
