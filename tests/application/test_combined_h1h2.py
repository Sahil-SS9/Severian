"""Handoffs 1 + 2 combined: which switch wins when several are on.

Decision (Kensei, 24/10/2026): with ``SEVERIAN_CONTEXT_FORMAT=v2`` on, the v2 renderer builds the whole block, so
``SEVERIAN_FILL_BUDGET`` and ``SEVERIAN_COMPACT_CITES`` do not apply. v2 already cites one kind of id per note (the
source observations), and fill under v2 is not designed yet. With v2 off, Handoff 1's switches behave exactly as on
their own branch. These tests pin both halves, so a later change to either cannot silently alter the other.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.worker import DerivationWorker
from severian.composition import build_bundle
from severian.domain.models import Scope

A = Scope(tenant_id="t", profile_id="alice", collection_id="profile", agent_id="a", session_id="s1")
TURNS = (  # the same three turns as test_context_efficiency.py
    "user: The sprint 1 deadline is April 1, 2024.\nassistant: Noted, April 1 it is.",
    "user: I moved the sprint 1 deadline to April 5, 2024.\nassistant: The deadline is now April 5.",
    "user: I set up the Trello board for sprint 1.\nassistant: Good, keep the cards moving.",
)


@dataclass
class PassThrough:
    """Saves each turn as one memory, word for word, like the default local derivation."""

    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content, "confidence": 0.9}]

SWITCHES = ("SEVERIAN_CONTEXT_FORMAT", "SEVERIAN_FILL_BUDGET", "SEVERIAN_COMPACT_CITES")


@pytest.fixture(autouse=True)
def clean_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in [key for key in os.environ if key.startswith("SEVERIAN_")]:
        monkeypatch.delenv(name, raising=False)


def _block(tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> str:
    """One fresh store per call, built AFTER the switches are set (the fill switch is read at construction)."""
    for key in SWITCHES:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    where = tmp_path / name
    where.mkdir()
    bundle = build_bundle(database=where / "s.db", fts=where / "s.fts", vectors=where / "s.vec")
    try:
        bundle.gateway = PassThrough()
        for turn, content in enumerate(TURNS, 1):
            bundle.service.ingest_observation(
                A, content=content,
                source={"session_id": "s0001", "turn": turn, "timestamp": "2024-03-15T09:00:00+00:00"})
        worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
        assert worker.drain(gateway=bundle.gateway).failed == 0
        hits = bundle.service.search(A, query="sprint 1 deadline", limit=1)
        assert hits
        return bundle.service.assemble_context(A, hits, max_chars=8000, query="sprint 1 deadline")
    finally:
        bundle.close()


def test_v2_wins_over_fill_and_compact(tmp_path, monkeypatch):
    v2_alone = _block(tmp_path, "v2", monkeypatch, {"SEVERIAN_CONTEXT_FORMAT": "v2"})
    v2_all = _block(tmp_path, "all", monkeypatch, {"SEVERIAN_CONTEXT_FORMAT": "v2", "SEVERIAN_FILL_BUDGET": "1",
                                                   "SEVERIAN_COMPACT_CITES": "1"})
    assert "say you don't know" in v2_alone and "Trello" not in v2_alone
    # Store ids differ between the two fresh stores; compare with ids masked.
    mask = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
    assert mask.sub("ID", v2_all) == mask.sub("ID", v2_alone)


def test_without_v2_the_handoff1_switches_still_act(tmp_path, monkeypatch):
    off = _block(tmp_path, "off", monkeypatch, {})
    h1 = _block(tmp_path, "h1", monkeypatch, {"SEVERIAN_FILL_BUDGET": "1", "SEVERIAN_COMPACT_CITES": "1"})
    assert "\nsource: " in off and "\nsource: " not in h1  # compact cites dropped the trailing line
    assert "Trello" not in off and "Trello" in h1  # fill spent the room on the next turn of the conversation
    assert "say you don't know" not in h1
