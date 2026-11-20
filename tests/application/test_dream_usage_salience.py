"""Usage never feeds a salience score, and the pin that keeps it that way.

``CurationService`` used to blend a usage term into salience
(``0.3 * min(usage_count / 100, 1)``). Nothing in ``src/`` ever passes
``search(track_usage=True)`` — the one call site's default is ``False`` — so every stored
``usage_count`` is 0 and the term always contributed exactly 0.0. Removing it is therefore
byte-identical on every real store, and this module pins both halves of that claim: the score no
longer moves when a count does, and the score equals the two-term blend the code documents.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from severian.application.dreaming import CurationService
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Memory, Observation, Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="s2sal", collection_id="profile", agent_id="s2sal", session_id="shared"
)


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def remember(bundle: ServiceBundle, content: str, *, usage_count: int = 0) -> Memory:
    observation = bundle.repository.add_observation(
        Observation(scope=SCOPE, content=content, raw_content=content, source={"uri": f"note://{uuid.uuid4().hex}"})
    )
    return bundle.repository.add_memory(
        Memory(
            scope=SCOPE,
            kind="fact",
            content=content,
            source_observation_ids=[observation.id],
            model_id="test",
            prompt_version="p1",
            confidence=0.9,
            usage_count=usage_count,
        )
    )


def salience_of(report, memory_id: str) -> float:
    return next(item["salience"] for item in report.scored if item["memory_id"] == memory_id)


def test_a_usage_count_does_not_change_the_salience_score(bundle: ServiceBundle) -> None:
    """The removed term, pinned: 100 recorded hits must score exactly as 0 does."""
    never_used = remember(bundle, "the user keeps a steel kettle")
    used_a_lot = remember(bundle, "the user keeps a copper pan", usage_count=100)
    report = CurationService(bundle.repository).curate(SCOPE)
    assert salience_of(report, never_used.id) == salience_of(report, used_a_lot.id)


def test_salience_is_the_documented_two_term_blend(bundle: ServiceBundle) -> None:
    """confidence * 0.4 + recency * 0.3, with no third input hiding in the number."""
    memory = remember(bundle, "the user keeps a steel kettle")
    service = CurationService(bundle.repository)
    report = service.curate(SCOPE)
    scored = next(item for item in report.scored if item["memory_id"] == memory.id)
    recency = 0.5 ** (scored["age_hours"] / 720.0)
    assert scored["salience"] == pytest.approx(0.4 * memory.confidence + 0.3 * recency, abs=5e-4)
    assert "usage" not in scored
