from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from severian.domain.models import Memory, Observation, Relation, Scope


def test_scope_rejects_empty_dimensions():
    with pytest.raises(ValidationError):
        Scope(tenant_id="", profile_id="p", collection_id="c", agent_id="a", session_id="s")


def test_observation_rejects_empty_content():
    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    with pytest.raises(ValidationError):
        Observation(scope=scope, content="")


def test_observation_rejects_malformed_hash():
    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    with pytest.raises(ValidationError):
        Observation(scope=scope, content="x", content_hash="not-a-sha256")
    with pytest.raises(ValidationError):
        Observation(scope=scope, content="x", raw_hash="ABC123")


def test_memory_requires_provenance_and_lineage():
    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    with pytest.raises(ValidationError):
        Memory(
            scope=scope,
            kind="fact",
            content="x",
            source_observation_ids=[],
            model_id="m",
            prompt_version="v1",
            confidence=0.5,
        )


# -- M1 R1: reinstating a support must not leave it in the withdrawn history -----


def _relation(supports: list[str], history: list[str]) -> Relation:
    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    return Relation(
        scope=scope,
        subject_id="subject",
        predicate="works_at",
        object_id="object",
        source_observation_ids=list(supports),
        retracted_for_ids=list(history),
        retracted_at=None if supports else datetime(2026, 11, 20, tzinfo=UTC),
    )


def test_reinstating_the_only_withdrawn_support_clears_its_withdrawn_history():
    """R1: a reinstated support cannot stay in ``retracted_for_ids``.

    Leaving it there makes the payload name one id as both current support and
    withdrawn support, which the strict snapshot validator refuses - so the store
    exports records it can no longer import.
    """
    relation = _relation(["obs-1"], [])
    withdrawn = relation.unsupported_by("obs-1", at=datetime(2026, 11, 20, 1, tzinfo=UTC))
    assert withdrawn is not None
    assert withdrawn.source_observation_ids == []
    assert withdrawn.retracted_for_ids == ["obs-1"]
    assert withdrawn.retracted_at is not None

    reinstated = withdrawn.supported_by("obs-1")
    assert reinstated is not None
    assert reinstated.source_observation_ids == ["obs-1"]
    assert reinstated.retracted_for_ids == []
    assert reinstated.retracted_at is None


def test_reinstating_one_support_keeps_the_other_withdrawn_history_in_order():
    """R1, partial case: other contributors keep the relation active, history keeps order."""
    relation = _relation(["obs-a", "obs-b"], ["obs-gone"])
    unsupported = relation.unsupported_by("obs-b", at=relation.created_at)
    assert unsupported is not None
    assert unsupported.source_observation_ids == ["obs-a"]
    assert unsupported.retracted_for_ids == ["obs-gone", "obs-b"]
    assert unsupported.retracted_at is None

    reinstated = unsupported.supported_by("obs-b")
    assert reinstated is not None
    assert reinstated.source_observation_ids == ["obs-a", "obs-b"]
    assert reinstated.retracted_for_ids == ["obs-gone"], "unrelated withdrawn history must survive in order"
    assert not set(reinstated.source_observation_ids) & set(reinstated.retracted_for_ids)
