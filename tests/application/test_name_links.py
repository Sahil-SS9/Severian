"""Round 2 idea 2: name links. A matched memory that names someone also brings in the other
memories that name them ("my manager is Kiran" -> "Kiran is allergic to sesame").

Off by default (link_seeds=0) so the switch can be measured alone and turned off.

Ported onto 0.3.11 from `0b091bd` (feat/round2-2026-10-05) for Handoff 2; the code it exercises had not moved.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from severian.application.retrieval import RetrievalService, SearchTuning, link_names
from severian.application.worker import DerivationWorker
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="octacon", collection_id="profile", agent_id="octacon", session_id="shared"
)
STRICT = 0.9  # keep only real keyword matches, so anything else must come from the links


@pytest.fixture(autouse=True)
def clean_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited SEVERIAN_* settings must never decide what this module measures."""
    for name in [key for key in os.environ if key.startswith("SEVERIAN_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def say(bundle: ServiceBundle, *turns: str) -> None:
    for number, turn in enumerate(turns):
        bundle.service.ingest_observation(SCOPE, content=turn, source={"session_id": f"chat-{number}"})
        assert DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(
            gateway=bundle.gateway
        ).failed == 0


def search(bundle: ServiceBundle, tuning: SearchTuning, query: str) -> list[str]:
    hits = RetrievalService(bundle.repository, bundle.lexical, bundle.vector, tuning).search(
        SCOPE, query, min_vector_score=STRICT
    )
    return [hit.memory.content for hit in hits]


TURNS = ("My manager is Kiran Mensah.", "Kiran told me they're allergic to sesame.", "The boiler was serviced.")


def test_off_by_default_and_from_environment(monkeypatch):
    assert SearchTuning().link_seeds == 0
    monkeypatch.setenv("SEVERIAN_LINK_SEEDS", "3")
    monkeypatch.setenv("SEVERIAN_LINK_WEIGHT", "0.4")
    tuning = SearchTuning.from_env()
    assert (tuning.link_seeds, tuning.link_weight) == (3, 0.4)


@pytest.mark.parametrize("bad", [{"link_seeds": -1}, {"link_weight": -0.1}])
def test_invalid_link_settings_are_rejected(bad):
    with pytest.raises(ValueError):
        SearchTuning(**bad)


def test_names_skip_dates_and_sentence_words():
    assert link_names("[2026/06/12 (Fri) 23:00] user: My manager is Kiran Mensah.") == ["Kiran Mensah"]
    assert link_names("Kiran told me in March.") == ["Kiran"]
    assert link_names("The user is pescatarian.") == []


def test_off_finds_only_the_keyword_match(bundle):
    say(bundle, *TURNS)
    assert search(bundle, SearchTuning(), "who is my manager") == ["My manager is Kiran Mensah."]


def test_links_bring_in_memories_that_name_the_same_person(bundle):
    say(bundle, *TURNS)
    found = search(bundle, SearchTuning(link_seeds=3), "who is my manager")
    assert found[0] == "My manager is Kiran Mensah."
    assert "Kiran told me they're allergic to sesame." in found
    assert "The boiler was serviced." not in found


def test_linked_memory_scores_below_its_match(bundle):
    say(bundle, *TURNS)
    hits = RetrievalService(bundle.repository, bundle.lexical, bundle.vector, SearchTuning(link_seeds=3)).search(
        SCOPE, "who is my manager", min_vector_score=STRICT
    )
    by_content = {hit.memory.content: hit.fused_score for hit in hits}
    assert by_content["Kiran told me they're allergic to sesame."] == pytest.approx(
        by_content["My manager is Kiran Mensah."] * 0.5
    )


def test_no_names_means_no_change(bundle):
    say(bundle, "The boiler was serviced.", "The boiler pressure is low.")
    assert search(bundle, SearchTuning(link_seeds=3), "boiler serviced") == search(
        bundle, SearchTuning(), "boiler serviced"
    )


OTHER = Scope(
    tenant_id="kensei", profile_id="dezzy", collection_id="profile", agent_id="dezzy", session_id="shared"
)


def test_links_never_cross_a_profile(bundle):
    """The link read is a keyword search: it is scope-bound, so another profile's memories can never appear."""
    say(bundle, *TURNS)
    for number, turn in enumerate(("Kiran told me the deploy key rotated.",)):
        bundle.service.ingest_observation(OTHER, content=turn, source={"session_id": f"chat-{number}"})
        assert DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(
            gateway=bundle.gateway
        ).failed == 0
    found = search(bundle, SearchTuning(link_seeds=3), "who is my manager")
    assert "Kiran told me they're allergic to sesame." in found, "control: the link fires inside the caller's profile"
    assert not any("deploy key" in item for item in found), "the other profile's memory was not linked in"
