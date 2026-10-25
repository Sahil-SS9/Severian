"""SEVERIAN_CONTEXT_CHARS: the Hermes provider's context cap can be set without code changes.

The provider is built by the Hermes loader with no arguments, so before 0.3.12 its 8,000-character cap could only be
changed by editing code. Unset keeps 8,000 exactly; an explicit constructor argument still wins; a bad value never
breaks memory, it falls back to the default with a warning.
"""
from __future__ import annotations

import logging
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
from severian.integrations.hermes import DEFAULT_CONTEXT_CHARS, SeverianMemoryProvider, context_chars_from_env


@dataclass
class PassThrough:
    """Saves each turn as one memory, word for word, like the default local derivation."""

    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content, "confidence": 0.9}]

LONG = "The quarterly review covers budgets, hiring, the Leeds office move and the new supplier contracts. " * 12


def _service(tmp_path: Path):
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
    return service, fts


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEVERIAN_CONTEXT_CHARS", raising=False)


def test_unset_keeps_the_8000_default():
    assert DEFAULT_CONTEXT_CHARS == 8_000
    assert context_chars_from_env() == 8_000


@pytest.mark.parametrize("raw, expected", [("16000", 16_000), (" 12000 ", 12_000), ("1000", 1_000)])
def test_valid_values_are_used(monkeypatch, raw, expected):
    monkeypatch.setenv("SEVERIAN_CONTEXT_CHARS", raw)
    assert context_chars_from_env() == expected


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "999", "16k", "1e5"])
def test_bad_values_fall_back_to_the_default_with_a_warning(monkeypatch, caplog, raw):
    monkeypatch.setenv("SEVERIAN_CONTEXT_CHARS", raw)
    with caplog.at_level(logging.WARNING):
        assert context_chars_from_env() == 8_000
    if raw.strip():
        assert "SEVERIAN_CONTEXT_CHARS" in caplog.text


def test_provider_reads_the_switch_and_an_explicit_argument_wins(tmp_path, monkeypatch):
    service, fts = _service(tmp_path)
    try:
        monkeypatch.setenv("SEVERIAN_CONTEXT_CHARS", "16000")
        assert SeverianMemoryProvider(service=service)._max_context_chars == 16_000
        assert SeverianMemoryProvider(service=service, max_context_chars=900)._max_context_chars == 900
        monkeypatch.delenv("SEVERIAN_CONTEXT_CHARS")
        assert SeverianMemoryProvider(service=service)._max_context_chars == 8_000
    finally:
        fts.close()


def test_prefetch_honours_the_configured_cap(tmp_path, monkeypatch):
    service, fts = _service(tmp_path)
    try:
        lengths = {}
        for cap in ("1000", "4000"):
            monkeypatch.setenv("SEVERIAN_CONTEXT_CHARS", cap)
            provider = SeverianMemoryProvider(service=service, gateway=PassThrough())
            provider.initialize(session_id=f"s{cap}", tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1")
            for i in range(6):
                provider.sync_turn(f"{i} {LONG}", "Noted.")
            block = provider.prefetch("quarterly review Leeds office", derive=True, limit=10)
            assert 0 < len(block) <= int(cap)
            lengths[cap] = len(block)
        assert lengths["4000"] > lengths["1000"]  # the larger cap really lets more through
    finally:
        fts.close()
