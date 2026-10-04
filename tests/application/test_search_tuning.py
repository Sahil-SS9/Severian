"""Search quality knobs (Step 2, next-improvements plan): fusion weights and conversation neighbours.

Every knob defaults to the behaviour before it existed, so each can be measured alone and switched off.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from severian.application.retrieval import RetrievalService, SearchTuning
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="octacon", collection_id="profile", agent_id="octacon", session_id="shared"
)


# Hash embeddings give every memory some similarity, so a strict floor keeps only real keyword
# matches; neighbour expansion is what adds the surrounding turns back.
STRICT = 0.9


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def say(bundle: ServiceBundle, session: str, *turns: str) -> None:
    for turn in turns:
        bundle.service.ingest_observation(SCOPE, content=turn, source={"session_id": session})
        worker = DerivationWorker(bundle.repository, bundle.derivation, bundle.projection)
        assert worker.drain(gateway=bundle.gateway).failed == 0


def retrieval(bundle: ServiceBundle, tuning: SearchTuning | None) -> RetrievalService:
    return RetrievalService(bundle.repository, bundle.lexical, bundle.vector, tuning)


# -- settings -----------------------------------------------------------------


def test_defaults_are_todays_behaviour():
    tuning = SearchTuning()
    assert (tuning.fusion_k, tuning.lexical_weight, tuning.vector_weight, tuning.neighbours) == (60, 1.0, 1.0, 0)


def test_environment_overrides_and_defaults(monkeypatch):
    for name in ("K", "LEXICAL", "VECTOR"):
        monkeypatch.delenv(f"SEVERIAN_FUSION_{name}", raising=False)
    monkeypatch.delenv("SEVERIAN_NEIGHBOURS", raising=False)
    assert SearchTuning.from_env() == SearchTuning()
    monkeypatch.setenv("SEVERIAN_FUSION_K", "20")
    monkeypatch.setenv("SEVERIAN_FUSION_LEXICAL", "2.5")
    monkeypatch.setenv("SEVERIAN_FUSION_VECTOR", "0.5")
    monkeypatch.setenv("SEVERIAN_NEIGHBOURS", "2")
    tuning = SearchTuning.from_env()
    assert (tuning.fusion_k, tuning.lexical_weight, tuning.vector_weight, tuning.neighbours) == (20, 2.5, 0.5, 2)


@pytest.mark.parametrize(
    "bad",
    [{"fusion_k": 0}, {"lexical_weight": -1.0}, {"vector_weight": -0.1}, {"neighbours": -1}, {"neighbour_seeds": 0}],
)
def test_invalid_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        SearchTuning(**bad)


# -- fusion -------------------------------------------------------------------


def test_default_fusion_is_plain_reciprocal_rank_fusion(bundle):
    say(bundle, "chat-1", "The deploy window is Friday morning")
    (hit,) = retrieval(bundle, SearchTuning()).search(SCOPE, "deploy window friday")
    assert hit.fused_score == pytest.approx(1 / 61 + 1 / 61)  # rank 1 in both lists, k=60, weights 1


def test_default_matches_pre_tuning_scores_exactly(bundle):
    say(bundle, "chat-1", "The deploy window is Friday morning", "The standup is at 09:15", "Alice likes green tea")
    query = "deploy standup tea"
    before = [(h.memory.id, h.fused_score) for h in retrieval(bundle, None).search(SCOPE, query)]
    after = [(h.memory.id, h.fused_score) for h in retrieval(bundle, SearchTuning()).search(SCOPE, query)]
    assert before == after and before


def test_zero_vector_weight_leaves_only_the_keyword_part(bundle):
    say(bundle, "chat-1", "The deploy window is Friday morning")
    (hit,) = retrieval(bundle, SearchTuning(vector_weight=0.0)).search(SCOPE, "deploy window friday")
    assert hit.fused_score == pytest.approx(1 / 61)


def test_weights_and_k_change_the_score(bundle):
    say(bundle, "chat-1", "The deploy window is Friday morning")
    (hit,) = retrieval(bundle, SearchTuning(fusion_k=10, lexical_weight=2.0, vector_weight=0.5)).search(
        SCOPE, "deploy window friday"
    )
    assert hit.fused_score == pytest.approx(2.0 / 11 + 0.5 / 11)


# -- conversation neighbours --------------------------------------------------

TURNS = (
    "Priya asked about the Lisbon flight",
    "The Lisbon flight leaves at 06:40 from Gatwick",
    "She will take the airport train the night before",
)


def contents(results) -> list[str]:
    return [item.memory.content for item in results]


def test_neighbours_off_returns_only_the_match(bundle):
    say(bundle, "trip", *TURNS)
    found = contents(retrieval(bundle, SearchTuning()).search(SCOPE, "Gatwick", min_vector_score=STRICT))
    assert found == [TURNS[1]]


def test_neighbours_add_the_turns_either_side_below_the_match(bundle):
    say(bundle, "trip", *TURNS)
    found = contents(retrieval(bundle, SearchTuning(neighbours=1)).search(SCOPE, "Gatwick", min_vector_score=STRICT))
    assert found[0] == TURNS[1]
    assert set(found) == set(TURNS)


def test_neighbours_stay_inside_their_own_conversation(bundle):
    say(bundle, "trip", *TURNS)
    say(bundle, "other", "Unrelated note about invoices", "Another unrelated note about printers")
    found = contents(retrieval(bundle, SearchTuning(neighbours=2)).search(SCOPE, "Gatwick", min_vector_score=STRICT))
    assert set(found) == set(TURNS)


def test_neighbour_window_is_bounded(bundle):
    say(bundle, "trip", "Opening line about packing", *TURNS, "Closing line about souvenirs")
    found = contents(retrieval(bundle, SearchTuning(neighbours=1)).search(SCOPE, "Gatwick", min_vector_score=STRICT))
    assert "Opening line about packing" not in found and "Closing line about souvenirs" not in found


def test_a_neighbour_that_also_matches_keeps_its_own_higher_score(bundle):
    say(bundle, "trip", *TURNS)
    def scores(tuning: SearchTuning) -> dict[str, float]:
        return {h.memory.content: h.fused_score for h in retrieval(bundle, tuning).search(SCOPE, "Lisbon flight")}

    plain, with_neighbours = scores(SearchTuning()), scores(SearchTuning(neighbours=1))
    for content, score in plain.items():
        assert with_neighbours[content] == score


def test_neighbours_respect_the_limit(bundle):
    say(bundle, "trip", *TURNS)
    found = retrieval(bundle, SearchTuning(neighbours=1)).search(SCOPE, "Gatwick", limit=2, min_vector_score=STRICT)
    assert len(found) == 2


def test_neighbour_expansion_does_nothing_without_a_session(bundle):
    for turn in TURNS:
        bundle.service.ingest_observation(SCOPE, content=turn, source={})
    DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    found = contents(retrieval(bundle, SearchTuning(neighbours=1)).search(SCOPE, "Gatwick", min_vector_score=STRICT))
    assert found == [TURNS[1]]
