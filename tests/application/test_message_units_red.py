"""S1 RED: current context packs whole turns and ignores message order."""
from __future__ import annotations

from pathlib import Path

from severian.application.worker import DerivationWorker
from severian.composition import build_bundle
from severian.domain.models import Collection, Scope


def _scope() -> Scope:
    return Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


def test_equal_budget_keeps_user_evidence_not_the_assistant_reply(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
    bundle = build_bundle(database=tmp_path / "r.db", fts=tmp_path / "r.fts", vectors=tmp_path / "r.vec")
    scope = _scope()
    bundle.repository.create_collection(Collection(id="c1", tenant_id="t1", name="S1 RED"))
    bundle.service.ingest_observation(
        scope,
        content=(
            "[2024/03/15 (Fri)] user: my new deadline is April 5\n"
            "assistant: " + "Thanks for the update. " * 80
        ),
        source={"turn": 1},
    )
    DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    hits = bundle.service.search(scope, query="deadline", limit=5)
    context = bundle.service.assemble_context(scope, hits, max_chars=300, query="deadline")
    bundle.close()
    assert "my new deadline is April 5" in context
    assert "assistant:" not in context


def test_same_date_updates_follow_message_order(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
    bundle = build_bundle(database=tmp_path / "o.db", fts=tmp_path / "o.fts", vectors=tmp_path / "o.vec")
    scope = _scope()
    bundle.repository.create_collection(Collection(id="c1", tenant_id="t1", name="S1 order"))
    for turn, text in ((3, "early: deadline April 1"), (8, "later: deadline April 5")):
        bundle.service.ingest_observation(
            scope,
            content=f"[2024/03/15 (Fri)] user: {text}",
            source={"turn": turn},
        )
    DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    hits = bundle.service.search(scope, query="deadline", limit=10)
    context = bundle.service.assemble_context(scope, hits, max_chars=8_000, query="deadline")
    bundle.close()
    assert context.index("early: deadline April 1") < context.index("later: deadline April 5")
    assert "later message" in context
