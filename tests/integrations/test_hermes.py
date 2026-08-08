"""Tests for the Hermes MemoryProvider compatibility adapter.

Covers lifecycle (initialize, teardown, session switch, delegation),
fail-closed scope behaviour, prefetch with source citations, sync_turn
writes, tool call routing, and cross-scope rejection.
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
    register,
)


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _scope_kwargs(**overrides: str) -> dict[str, str]:
    """Scope dimensions passed to initialize() as kwargs.

    session_id is passed as the explicit first argument to initialize(),
    so it is NOT included here to avoid 'multiple values' collision.
    """
    base = {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
    }
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


@pytest.fixture
def service_and_provider(tmp_path: Path):
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
    provider = SeverianMemoryProvider(service=service, gateway=FakeGateway())
    yield service, provider, repo
    fts.close()
    vector.close()
    repo.close()


# -- lifecycle -----------------------------------------------------------

def test_name_returns_severian(service_and_provider):
    _, provider, _ = service_and_provider
    assert provider.name == "severian"


def test_is_available_returns_true_when_healthy(service_and_provider):
    _, provider, _ = service_and_provider
    assert provider.is_available() is True


def test_initialize_establishes_scope(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs(agent_id="a1"))
    # Should not raise; scope is established.
    assert provider._scope is not None
    assert provider._scope.session_id == "s1"


def test_initialize_fails_closed_on_missing_scope(service_and_provider):
    _, provider, _ = service_and_provider
    with pytest.raises(ScopeNotEstablishedError):
        provider.initialize(session_id="s1", tenant_id="t1")  # missing profile, collection, agent
    # Fail closed: scope is cleared.
    assert provider._scope is None


def test_initialize_fails_closed_on_blank_scope(service_and_provider):
    _, provider, _ = service_and_provider
    with pytest.raises(ScopeNotEstablishedError):
        provider.initialize(session_id="s1", **_scope_kwargs(profile_id="   "))
    assert provider._scope is None


def test_teardown_clears_scope(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    assert provider._scope is not None
    provider.teardown()
    assert provider._scope is None
    assert provider._session_id is None


def test_shutdown_clears_scope_without_closing_service(service_and_provider):
    service, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.shutdown()
    assert provider._scope is None
    assert provider._session_id is None
    assert service.health().ready is True


def test_on_session_switch_re_establishes_scope(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.on_session_switch(new_session_id="s2", tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1")
    assert provider._scope is not None
    assert provider._scope.session_id == "s2"


def test_on_session_switch_fails_closed_on_incomplete_scope(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    with pytest.raises(ScopeNotEstablishedError):
        provider.on_session_switch(new_session_id="s2", tenant_id="t1")  # incomplete
    # After failed switch, scope is cleared (fail closed).
    assert provider._scope is None


def test_on_delegation_records_in_parent_scope(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs(agent_id="a1"))
    provider.on_delegation(
        "research task",
        "research result",
        child_session_id="child-s1",
        delegatee="delegate-agent",
    )
    assert provider._scope is not None
    assert provider._scope.agent_id == "a1"


def test_on_delegation_requires_active_scope(service_and_provider):
    _, provider, _ = service_and_provider
    with pytest.raises(ScopeNotEstablishedError):
        provider.on_delegation("research task", "research result")


# -- prompt + context ----------------------------------------------------

def test_system_prompt_block_is_nonempty(service_and_provider):
    _, provider, _ = service_and_provider
    block = provider.system_prompt_block()
    assert "severian" in block.lower()
    assert len(block) > 20


def test_prefetch_returns_empty_without_data(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    context = provider.prefetch("anything")
    assert context == ""


def test_prefetch_returns_source_cited_context(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.sync_turn("Ada founded a company", "")
    context = provider.prefetch("Ada", derive=True, limit=5)
    assert context
    assert "source:" in context  # citation marker from ContextAssembler


def test_prefetch_without_scope_fails_closed(service_and_provider):
    _, provider, _ = service_and_provider
    with pytest.raises(ScopeNotEstablishedError):
        provider.prefetch("Ada")


# -- sync_turn ------------------------------------------------------------

def test_sync_turn_writes_observation(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.sync_turn("Ada founded a company", "Assistant response", turn=1)
    # Verify it was written by fetching via tool call.
    context = provider.prefetch("Ada", derive=True)
    assert "Ada founded a company" in context or "Fact: Ada" in context


def test_sync_turn_attaches_safe_source_metadata(service_and_provider):
    service, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.sync_turn("test content", "Assistant response", turn=3)
    # Inspect the stored observation directly via the service.
    observations = service._repository.list_observations(provider._scope)  # type: ignore[attr-defined]
    assert len(observations) >= 1
    obs = observations[-1]
    assert obs.source["provider"] == "severian"
    assert obs.source["turn"] == 3


def test_sync_turn_without_scope_fails_closed(service_and_provider):
    _, provider, _ = service_and_provider
    with pytest.raises(ScopeNotEstablishedError):
        provider.sync_turn("content", "")


# -- tools ---------------------------------------------------------------

def test_get_tool_schemas_returns_four_tools(service_and_provider):
    _, provider, _ = service_and_provider
    schemas = provider.get_tool_schemas()
    names = {s["name"] for s in schemas}
    assert names == {
        "severian_search",
        "severian_ingest",
        "severian_get_observation",
        "severian_search_shared",
    }
    for schema in schemas:
        assert "parameters" in schema
        assert set(schema["parameters"].get("required", [])) >= {
            "tenant_id",
            "profile_id",
            "collection_id",
            "agent_id",
            "session_id",
        }


def test_handle_tool_call_search(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.sync_turn("Ada founded a company", "")
    result = json.loads(provider.handle_tool_call(
        "severian_search", {**_tool_scope_args(), "query": "Ada", "limit": 5}
    ))
    assert "hits" in result
    assert isinstance(result["hits"], list)


def test_tool_schemas_include_search_shared(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    names = {t["name"] for t in provider.get_tool_schemas()}
    assert "severian_search_shared" in names
    # The shared tool must require agent_ids.
    shared = next(t for t in provider.get_tool_schemas() if t["name"] == "severian_search_shared")
    assert "agent_ids" in shared["parameters"]["required"]


def test_handle_tool_call_search_shared(service_and_provider):
    # Two agents in the same profile/collection share memory via the seam.
    # In the fleet each profile runs its own provider with its own scope; the
    # shared read surfaces another agent's memory when explicitly asked.
    service, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.sync_turn("agent-a1 planning docs", "")

    # Seed agent a2's memory directly through the service (the tool surface
    # correctly refuses to cross scope, so a2 is populated out-of-band).
    a2_scope = Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1",
        agent_id="a2", session_id="s1",
    )
    service.ingest_observation(a2_scope, content="agent-a2 status update",
                               source={"uri": "test://a2"})
    service.search(a2_scope, query="agent-a2", limit=5, derive=True,
                   gateway=FakeGateway())

    result = json.loads(provider.handle_tool_call(
        "severian_search_shared",
        {**_tool_scope_args(), "agent_ids": ["a2"], "query": "agent-a2", "limit": 5},
    ))
    assert "hits" in result
    contents = [h["content"] for h in result["hits"]]
    assert any("agent-a2 status update" in c for c in contents)


def test_handle_tool_call_search_shared_rejects_empty_agent_ids(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    result = json.loads(provider.handle_tool_call(
        "severian_search_shared",
        {**_tool_scope_args(), "agent_ids": [], "query": "anything"},
    ))
    assert "error" in result
    assert "non-empty" in result["error"]


def test_handle_tool_call_ingest(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    result = json.loads(provider.handle_tool_call(
        "severian_ingest", {**_tool_scope_args(), "content": "new fact", "source": {"uri": "test"}}
    ))
    assert "observation_id" in result
    assert result["content"] == "new fact"
    assert result["job_id"]


def test_handle_tool_call_get_observation(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    ingest_result = json.loads(
        provider.handle_tool_call("severian_ingest", {**_tool_scope_args(), "content": "fetch me"})
    )
    obs_id = ingest_result["observation_id"]
    result = json.loads(provider.handle_tool_call(
        "severian_get_observation", {**_tool_scope_args(), "observation_id": obs_id}
    ))
    assert result["observation_id"] == obs_id
    assert result["content"] == "fetch me"


def test_handle_tool_call_unknown_tool(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    result = json.loads(provider.handle_tool_call("nonexistent", _tool_scope_args()))
    assert "error" in result
    assert "unknown tool" in result["error"]


def test_handle_tool_call_search_empty_query(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    result = json.loads(provider.handle_tool_call("severian_search", {**_tool_scope_args(), "query": "  "}))
    assert "error" in result


def test_handle_tool_call_without_scope_fails_closed(service_and_provider):
    _, provider, _ = service_and_provider
    with pytest.raises(ScopeNotEstablishedError):
        provider.handle_tool_call("severian_search", {"query": "Ada"})


# -- cross-scope rejection ------------------------------------------------

def test_cross_scope_observation_not_found(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    ingest_result = json.loads(
        provider.handle_tool_call("severian_ingest", {**_tool_scope_args(), "content": "secret"})
    )
    obs_id = ingest_result["observation_id"]
    # Switch to a different profile scope.
    provider.initialize(session_id="s1", **_scope_kwargs(profile_id="p2"))
    result = json.loads(provider.handle_tool_call(
        "severian_get_observation", {**_tool_scope_args(profile_id="p2"), "observation_id": obs_id}
    ))
    assert "error" in result
    assert "not found" in result["error"]


# -- no resource ownership ------------------------------------------------

def test_teardown_does_not_close_service_resources(service_and_provider):
    service, provider, repo = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    provider.teardown()
    # The repository should still be usable (not closed).
    report = service.health()
    assert report.ready is True
    # Verify the underlying repository engine is alive.
    engine = getattr(repo, "engine", None)
    if engine is not None:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")


def test_tool_call_requires_explicit_matching_scope(service_and_provider):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())
    missing = json.loads(provider.handle_tool_call("severian_search", {"query": "Ada"}))
    assert missing["error"] == "scope is required"
    mismatched = json.loads(provider.handle_tool_call(
        "severian_search", {**_tool_scope_args(profile_id="p2"), "query": "Ada"}
    ))
    assert mismatched["error"] == "scope mismatch"


def test_invalid_tool_scope_is_rejected_before_service_access(service_and_provider, monkeypatch):
    _, provider, _ = service_and_provider
    provider.initialize(session_id="s1", **_scope_kwargs())

    def fail(*args, **kwargs):
        raise AssertionError("service must not be touched")

    monkeypatch.setattr(provider._service, "search", fail)
    result = json.loads(provider.handle_tool_call("severian_search", {"query": "Ada"}))
    assert result == {"error": "scope is required"}


# -- P1-4: zero-arg construction and plugin entry -------------------------

def test_zero_arg_construction_builds_service_from_env(tmp_path, monkeypatch):
    # P1-4 repro: the real Hermes loader instantiates providers with a
    # zero-arg constructor; an injected-only provider could never load.
    monkeypatch.setenv("SEVERIAN_STORAGE", str(tmp_path))
    monkeypatch.delenv("SEVERIAN_DSN", raising=False)
    provider = SeverianMemoryProvider()
    try:
        assert provider.is_available() is True
        provider.initialize(session_id="s1", **_scope_kwargs())
        result = json.loads(
            provider.handle_tool_call(
                "severian_ingest", {**_tool_scope_args(), "content": "plugin probe"}
            )
        )
        assert "observation_id" in result
    finally:
        provider.shutdown()


def test_zero_arg_construction_rejects_blank_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("SEVERIAN_STORAGE", str(tmp_path))
    monkeypatch.delenv("SEVERIAN_DSN", raising=False)
    provider = SeverianMemoryProvider()
    try:
        with pytest.raises(ScopeNotEstablishedError):
            provider.initialize(session_id="s1")  # no scope dimensions
    finally:
        provider.shutdown()


def test_register_entry_point_hands_provider_to_context(tmp_path, monkeypatch):
    # P1-4: the Hermes loader calls register(ctx) with a context exposing
    # register_memory_provider; the plugin must hand back a usable
    # provider instance without an injected service.
    monkeypatch.setenv("SEVERIAN_STORAGE", str(tmp_path))
    monkeypatch.delenv("SEVERIAN_DSN", raising=False)

    class Collector:
        def __init__(self):
            self.provider = None

        def register_memory_provider(self, provider):
            self.provider = provider

    collector = Collector()
    register(collector)
    assert collector.provider is not None
    assert collector.provider.name == "severian"
    collector.provider.shutdown()


def test_resolve_embedding_default_is_hash(monkeypatch):
    # P1: with SEVERIAN_EMBEDDING unset (or any non-granite value), the
    # zero-arg env path must keep the deterministic hash embedding.
    from severian.infrastructure.vector import HashEmbedding
    from severian.integrations.hermes import _resolve_embedding

    monkeypatch.delenv("SEVERIAN_EMBEDDING", raising=False)
    assert isinstance(_resolve_embedding(), HashEmbedding)

    monkeypatch.setenv("SEVERIAN_EMBEDDING", "some-other-backend")
    assert isinstance(_resolve_embedding(), HashEmbedding)


def test_resolve_embedding_granite_selects_neural_or_hash(monkeypatch):
    # P1: SEVERIAN_EMBEDDING=granite must select the Granite provider
    # (dimension 384), degrading safely to hash when the neural deps /
    # model are unavailable in this environment.
    from severian.infrastructure.granite_embedding import GraniteR2Embedding
    from severian.infrastructure.vector import HashEmbedding
    from severian.integrations.hermes import _resolve_embedding

    monkeypatch.setenv("SEVERIAN_EMBEDDING", "granite")
    embedding = _resolve_embedding()
    # or_hash() returns the neural provider when loaded, else hash at
    # the same dimension. Either way it must expose dimension == 384.
    assert isinstance(embedding, (GraniteR2Embedding, HashEmbedding))
    assert embedding.dimension == 384


def test_build_from_env_selects_embedding_from_env(tmp_path, monkeypatch):
    # P1 end-to-end: _build_default_service_from_env wires the env-selected
    # embedding into the constructed bundle's vector index.
    from severian.infrastructure.vector import HashEmbedding
    from severian.integrations.hermes import (
        _build_default_service_from_env,
        provider_bundles,
    )

    monkeypatch.setenv("SEVERIAN_STORAGE", str(tmp_path))
    monkeypatch.delenv("SEVERIAN_DSN", raising=False)
    monkeypatch.delenv("SEVERIAN_EMBEDDING", raising=False)

    before = len(provider_bundles)
    service = _build_default_service_from_env()
    try:
        assert isinstance(provider_bundles[-1].vector.embedding, HashEmbedding)
        assert provider_bundles[-1].service is service
    finally:
        provider_bundles[-1].close()
        del provider_bundles[before:]
