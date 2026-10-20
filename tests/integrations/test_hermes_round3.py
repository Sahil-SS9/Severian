"""Round 3 fixes through the Hermes plugin: prefetch passes the question to the context builder.

Without the question, fix (a) cannot tell that "what did you suggest" needs the assistant's replies, and fix (b)
cannot tell whether a disagreement is about what was asked.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.domain.models import Collection
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex
from severian.integrations.hermes import SeverianMemoryProvider

PLAN = "Here is a long plan with many steps. " * 30


@dataclass
class PassThrough:
    """Saves each turn as one memory, word for word, like the default local derivation."""

    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content, "confidence": 0.9}]


@pytest.fixture
def provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SeverianMemoryProvider]:
    monkeypatch.setenv("SEVERIAN_REPLY_TRIM", "1")
    monkeypatch.setenv("SEVERIAN_CONFLICT_NOTES", "1")
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts.db")
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=ProjectionCoordinator(repo, fts, vector),
        derivation=DerivationService(repo),
        context=ContextAssembler(),
    )
    built = SeverianMemoryProvider(service=service, gateway=PassThrough(), max_context_chars=900)
    built.initialize(session_id="s1", tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1")
    yield built
    fts.close()
    vector.close()
    repo.close()


def test_prefetch_drops_replies_first_when_space_is_short_unless_the_question_asks_for_them(provider) -> None:
    provider.sync_turn("I moved to Leeds in May for the new job.", PLAN)
    provider.sync_turn("I started at the Leeds office on 3 June.", PLAN)
    short = provider.prefetch("When did I move to Leeds?", derive=True, limit=10)
    assert "user: I moved to Leeds in May for the new job." in short
    assert "user: I started at the Leeds office on 3 June." in short
    assert "Here is a long plan" not in short
    assert "Here is a long plan" in provider.prefetch("What plan did you suggest for Leeds?", derive=True, limit=10)


def test_prefetch_notes_a_disagreement_only_when_it_is_about_the_question(provider) -> None:
    provider.sync_turn("I've never completed any coin toss problems before.", "ok")
    provider.sync_turn("I completed 5 coin toss problems today and got 4 right.", "Nice work.")
    assert "Memories that disagree" in provider.prefetch("Have I done coin toss problems?", derive=True, limit=10)
    assert "Memories that disagree" not in provider.prefetch("What did I eat for lunch?", derive=True, limit=10)
