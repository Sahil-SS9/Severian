import pytest
from pydantic import ValidationError

from severian.domain.models import Memory, Observation, Scope


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
