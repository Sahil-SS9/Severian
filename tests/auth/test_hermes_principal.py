"""Tests for Hermes integration construct-time principal binding (SEC-01).

RED-first: ``SeverianMemoryProvider`` optionally accepts a principal
Scope at construction. When bound, the provider's scope is fixed to
the principal and ``initialize`` / tool calls cannot switch
tenant/profile.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex
from severian.integrations.hermes import (
    ScopeNotEstablishedError,
    SeverianMemoryProvider,
)


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _principal() -> Scope:
    return Scope(
        tenant_id="t1",
        profile_id="p1",
        collection_id="c1",
        agent_id="a1",
        session_id="s1",
    )


def _build_service(tmp_path: Path):
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
    return service, repo, fts, vector


def _scope_kwargs(**overrides: str) -> dict[str, str]:
    base = {"tenant_id": "t1", "profile_id": "p1", "collection_id": "c1", "agent_id": "a1"}
    base.update(overrides)
    return base


def _tool_scope_args(**overrides: str) -> dict[str, str]:
    base = {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }
    base.update(overrides)
    return base


# -- without principal: existing behaviour preserved ----------------------

def test_provider_without_principal_uses_initialize_scope(tmp_path: Path):
    service, repo, fts, vector = _build_service(tmp_path)
    provider = SeverianMemoryProvider(service=service, gateway=FakeGateway())
    provider.initialize(session_id="s1", **_scope_kwargs())
    assert provider._scope is not None
    assert provider._scope.tenant_id == "t1"
    fts.close()
    vector.close()
    repo.close()


# -- with principal: construct-time binding -------------------------------

@pytest.fixture
def principal_provider(tmp_path: Path):
    service, repo, fts, vector = _build_service(tmp_path)
    provider = SeverianMemoryProvider(service=service, gateway=FakeGateway(), principal=_principal())
    yield provider
    fts.close()
    vector.close()
    repo.close()


def test_provider_with_principal_scope_is_pre_established(principal_provider):
    """The principal binds the scope at construction — no initialize needed."""
    assert principal_provider._scope is not None
    assert principal_provider._scope.tenant_id == "t1"


def test_provider_with_principal_initialize_matches(principal_provider):
    """initialize() with matching tenant/profile is accepted."""
    principal_provider.initialize(session_id="s1", **_scope_kwargs())
    assert principal_provider._scope.tenant_id == "t1"


def test_provider_with_principal_rejects_cross_tenant_initialize(principal_provider):
    """initialize() claiming a different tenant must fail closed."""
    with pytest.raises(ScopeNotEstablishedError):
        principal_provider.initialize(
            session_id="s1",
            **_scope_kwargs(tenant_id="evil"),
        )
    # Fail closed: scope cleared.
    assert principal_provider._scope is None


def test_provider_with_principal_rejects_cross_profile_initialize(principal_provider):
    with pytest.raises(ScopeNotEstablishedError):
        principal_provider.initialize(
            session_id="s1",
            **_scope_kwargs(profile_id="other"),
        )


def test_provider_with_principal_tool_call_rejects_cross_tenant(principal_provider):
    result = json.loads(
        principal_provider.handle_tool_call(
            "severian_ingest",
            {**_tool_scope_args(tenant_id="evil"), "content": "x"},
        )
    )
    assert "error" in result
    assert "scope" in result["error"].lower() or "principal" in result["error"].lower()


def test_provider_with_principal_tool_call_accepts_matching(principal_provider):
    result = json.loads(
        principal_provider.handle_tool_call(
            "severian_ingest",
            {**_tool_scope_args(), "content": "ok"},
        )
    )
    assert "observation_id" in result


def test_provider_with_principal_collection_is_arg_selectable(principal_provider):
    """collection_id / agent_id / session_id remain client-selectable."""
    result = json.loads(
        principal_provider.handle_tool_call(
            "severian_ingest",
            {**_tool_scope_args(collection_id="c2", agent_id="a2", session_id="s2"), "content": "ok"},
        )
    )
    assert "observation_id" in result
