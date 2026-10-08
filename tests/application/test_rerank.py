"""Round 2 improvement 4, rerank: fetch about 30 candidates, re-score them with a cross-encoder, keep the best.

Behind ``SEVERIAN_RERANK`` (a cross-encoder model name or path; unset = search exactly as before). The model
is optional (sentence-transformers); these tests use a fake scorer so they need neither the model nor a GPU.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from severian.application.rerank import RERANK_POOL, rerank_model, reranked
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

SCOPE = Scope(tenant_id="kensei", profile_id="sahil", collection_id="profile", agent_id="kensei", session_id="s1")


class Scorer:
    """Scores a text by whether it holds the magic word; records every call."""

    def __init__(self, word: str = "Anytime") -> None:
        self.word, self.calls = word, []

    def score(self, query: str, texts: list[str]) -> list[float]:
        self.calls.append((query, list(texts)))
        return [1.0 if self.word in t else 0.0 for t in texts]


def test_the_switch_is_off_unless_a_model_is_named(monkeypatch):
    monkeypatch.delenv("SEVERIAN_RERANK", raising=False)
    assert rerank_model() is None
    monkeypatch.setenv("SEVERIAN_RERANK", "cross-encoder/ms-marco-MiniLM-L6-v2")
    assert rerank_model() == "cross-encoder/ms-marco-MiniLM-L6-v2"


def test_reranked_orders_by_score_and_keeps_ties_in_the_old_order():
    items = ["a", "b Anytime", "c", "d Anytime"]
    assert reranked("gym?", items, Scorer(), text=lambda x: x) == ["b Anytime", "d Anytime", "a", "c"]


def test_only_the_top_pool_is_rescored_the_rest_keeps_its_place():
    items = [f"m{i}" for i in range(RERANK_POOL + 5)] + []
    items[RERANK_POOL + 2] = "late Anytime"  # outside the pool: never promoted
    scorer = Scorer()
    out = reranked("gym?", items, scorer, text=lambda x: x)
    assert len(scorer.calls[0][1]) == RERANK_POOL
    assert out[RERANK_POOL + 2] == "late Anytime"
    assert out[:RERANK_POOL] == items[:RERANK_POOL]


def test_a_scorer_failure_falls_back_to_the_original_order():
    class Broken:
        def score(self, query: str, texts: list[str]) -> list[float]:
            raise RuntimeError("model gone")

    items = ["a", "b"]
    assert reranked("q", items, Broken(), text=lambda x: x) == items


def test_a_wrong_length_reply_falls_back_to_the_original_order():
    class Short:
        def score(self, query: str, texts: list[str]) -> list[float]:
            return [1.0]

    assert reranked("q", ["a", "b"], Short(), text=lambda x: x) == ["a", "b"]


# -- wiring -----------------------------------------------------------------------------------------------


def build(tmp_path: Path) -> ServiceBundle:
    tmp_path.mkdir(parents=True, exist_ok=True)
    return build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")


def fill(bundle: ServiceBundle) -> None:
    for i, text in enumerate(
        ["I go to the gym on Mondays.", "My gym is great.", "Gym day today.", "I joined Anytime Fitness."], 1
    ):
        bundle.service.ingest_observation(SCOPE, content=f"user: {text}\nassistant: ok", source={"turn": i})
    bundle.service.search(SCOPE, query="gym", derive=True, gateway=bundle.gateway)


def test_with_a_reranker_the_service_puts_its_best_first(tmp_path):
    bundle = build(tmp_path)
    try:
        fill(bundle)
        bundle.service._reranker = Scorer()
        hits = bundle.service.search(SCOPE, query="Which gym am I a member of?", limit=2)
        assert "Anytime" in hits[0].content
    finally:
        bundle.close()


def test_without_a_reranker_search_is_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("SEVERIAN_RERANK", raising=False)
    bundle = build(tmp_path)
    try:
        fill(bundle)
        assert bundle.service._reranker is None
        before = [h.memory_id for h in bundle.service.search(SCOPE, query="gym", limit=3)]
        assert before == [h.memory_id for h in bundle.service.search(SCOPE, query="gym", limit=3)]
    finally:
        bundle.close()


def test_the_bundle_builds_a_reranker_only_when_the_switch_names_a_model(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(
        "severian.composition.load_reranker", lambda model, device, max_tokens=None: made.append(model) or Scorer()
    )
    monkeypatch.setenv("SEVERIAN_RERANK", "some/model")
    bundle = build(tmp_path)
    try:
        assert made == ["some/model"] and bundle.service._reranker is not None
    finally:
        bundle.close()


@pytest.mark.parametrize("limit", [1, 10])
def test_rerank_never_returns_more_than_asked(tmp_path, limit):
    bundle = build(tmp_path)
    try:
        fill(bundle)
        bundle.service._reranker = Scorer()
        assert len(bundle.service.search(SCOPE, query="gym", limit=limit)) <= limit
    finally:
        bundle.close()


# -- loading: a setting for the device, once per process, never a crash -----------------------------------


def test_the_device_is_a_setting(monkeypatch):
    from severian.application import rerank

    monkeypatch.delenv("SEVERIAN_RERANK_DEVICE", raising=False)
    assert rerank.rerank_device() is None
    monkeypatch.setenv("SEVERIAN_RERANK_DEVICE", "cuda:1")
    assert rerank.rerank_device() == "cuda:1"


def test_the_model_loads_once_per_process(monkeypatch):
    from severian.application import rerank

    loads = []
    monkeypatch.setattr(
        rerank, "_load", lambda model, device, max_tokens=None: loads.append((model, device)) or object()
    )
    rerank.load_reranker.cache_clear()
    try:
        assert rerank.load_reranker("m", "cuda:1") is rerank.load_reranker("m", "cuda:1")
        assert loads == [("m", "cuda:1")]
    finally:
        rerank.load_reranker.cache_clear()


def test_a_device_that_fails_falls_back_to_the_cpu_with_a_warning(monkeypatch, caplog):
    from severian.application import rerank

    def load(model, device, max_tokens=None):
        if device != "cpu":
            raise RuntimeError("CUDA out of memory")
        return "cpu-model"

    monkeypatch.setattr(rerank, "_load", load)
    rerank.load_reranker.cache_clear()
    try:
        assert rerank.load_reranker("m", "cuda:1") == "cpu-model"
        assert "falling back to the CPU" in caplog.text
    finally:
        rerank.load_reranker.cache_clear()


def test_when_nothing_loads_there_is_no_rerank_and_a_warning(monkeypatch, caplog):
    from severian.application import rerank

    def load(model, device, max_tokens=None):
        raise RuntimeError("no model")

    monkeypatch.setattr(rerank, "_load", load)
    rerank.load_reranker.cache_clear()
    try:
        assert rerank.load_reranker("m", "cuda:1") is None
        assert "searching without rerank" in caplog.text
    finally:
        rerank.load_reranker.cache_clear()


def test_a_load_failure_never_breaks_building_severian(tmp_path, monkeypatch):
    from severian.application import rerank

    def load(model, device, max_tokens=None):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(rerank, "_load", load)
    monkeypatch.setenv("SEVERIAN_RERANK", "some/model")
    rerank.load_reranker.cache_clear()
    try:
        bundle = build(tmp_path)
        try:
            fill(bundle)
            assert bundle.service._reranker is None
            assert bundle.service.search(SCOPE, query="gym", limit=2)
        finally:
            bundle.close()
    finally:
        rerank.load_reranker.cache_clear()


# -- trim: how many candidates, how long each text (round 2 improvement 4, 08/10) -------------------------


def test_pool_size_and_text_length_are_settings(monkeypatch):
    from severian.application import rerank

    monkeypatch.delenv("SEVERIAN_RERANK_POOL", raising=False)
    monkeypatch.delenv("SEVERIAN_RERANK_MAX_TOKENS", raising=False)
    assert rerank.rerank_pool() == RERANK_POOL and rerank.rerank_max_tokens() is None
    monkeypatch.setenv("SEVERIAN_RERANK_POOL", "15")
    monkeypatch.setenv("SEVERIAN_RERANK_MAX_TOKENS", "128")
    assert rerank.rerank_pool() == 15 and rerank.rerank_max_tokens() == 128


@pytest.mark.parametrize("bad", ["0", "-3", "lots"])
def test_a_bad_pool_setting_falls_back_to_the_default(monkeypatch, bad):
    from severian.application import rerank

    monkeypatch.setenv("SEVERIAN_RERANK_POOL", bad)
    assert rerank.rerank_pool() == RERANK_POOL


def test_a_smaller_pool_rescores_only_that_many():
    items = [f"m{i}" for i in range(20)]
    scorer = Scorer()
    out = reranked("gym?", items, scorer, text=lambda x: x, pool=10)
    assert len(scorer.calls[0][1]) == 10 and out[10:] == items[10:]


def test_the_text_length_reaches_the_model_loader(monkeypatch):
    from severian.application import rerank

    seen = []
    monkeypatch.setattr(rerank, "_load", lambda model, device, max_tokens=None: seen.append(max_tokens) or object())
    rerank.load_reranker.cache_clear()
    try:
        rerank.load_reranker("m", "cpu", 128)
        assert seen == [128]
    finally:
        rerank.load_reranker.cache_clear()


def test_the_service_uses_the_pool_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVERIAN_RERANK_POOL", "2")
    bundle = build(tmp_path)
    try:
        fill(bundle)
        scorer = Scorer()
        bundle.service._reranker = scorer
        bundle.service.search(SCOPE, query="Which gym?", limit=4)
        assert len(scorer.calls[0][1]) == 2
    finally:
        bundle.close()
