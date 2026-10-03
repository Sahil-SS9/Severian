"""Hermes fleet wiring: scope derived from what Hermes passes today.

Since the 6 Sep 2026 upstream merge, Hermes passes ``initialize`` only
``session_id``, ``platform``, ``hermes_home``, ``agent_context``,
``agent_identity`` and ``agent_workspace`` (plus optional gateway identity).
The host-side hand-off of tenant/profile/collection/agent was dropped, so
every profile failed closed. These tests pin the provider-side derivation
that replaces it, plus:

- profile-wide recall: a profile remembers across its chats, and the chat id
  is kept as provenance instead of being a scope wall;
- isolation: one profile never sees another profile's memory;
- write suppression for non-primary contexts (cron, subagent, flush);
- the default shared store location when ``SEVERIAN_STORAGE`` is unset.
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
from severian.domain.models import Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex
from severian.integrations.hermes import (
    HERMES_PROFILE_COLLECTION,
    HERMES_PROFILE_SESSION,
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


def _hermes_kwargs(identity: str, **overrides: object) -> dict[str, object]:
    """Exactly what KenseiAgent's _memory_provider_init_kwargs passes today."""
    base: dict[str, object] = {
        "platform": "discord",
        "hermes_home": f"/home/kensei/.hermes/profiles/{identity}",
        "agent_context": "primary",
        "agent_identity": identity,
        "agent_workspace": "hermes",
    }
    base.update(overrides)
    return base


@pytest.fixture
def service(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    fts = FTS5Index(tmp_path / "fts.db")
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    svc = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=ProjectionCoordinator(repo, fts, vector),
        derivation=DerivationService(repo),
        context=ContextAssembler(),
    )
    yield svc, repo
    fts.close()
    vector.close()
    repo.close()


def _provider(svc) -> SeverianMemoryProvider:
    return SeverianMemoryProvider(service=svc, gateway=FakeGateway())


# -- scope derivation ------------------------------------------------------

def test_initialize_derives_scope_from_agent_identity(service, monkeypatch):
    monkeypatch.delenv("SEVERIAN_TENANT", raising=False)
    svc, _ = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    assert provider._scope == Scope(
        tenant_id="kensei",
        profile_id="octacon",
        collection_id=HERMES_PROFILE_COLLECTION,
        agent_id="octacon",
        session_id=HERMES_PROFILE_SESSION,
    )


def test_tenant_can_be_overridden_by_env(service, monkeypatch):
    monkeypatch.setenv("SEVERIAN_TENANT", "acme")
    svc, _ = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    assert provider._scope is not None
    assert provider._scope.tenant_id == "acme"


def test_blank_agent_identity_fails_closed(service):
    svc, _ = service
    provider = _provider(svc)
    with pytest.raises(ScopeNotEstablishedError):
        provider.initialize("chat-1", **_hermes_kwargs("   "))
    assert provider._scope is None


def test_partial_explicit_scope_is_not_mixed_with_derivation(service):
    # Any explicit dimension selects the explicit contract: all four are then
    # required. A half-explicit scope must never be completed by guessing.
    svc, _ = service
    provider = _provider(svc)
    with pytest.raises(ScopeNotEstablishedError):
        provider.initialize("chat-1", tenant_id="kensei", **_hermes_kwargs("octacon"))
    assert provider._scope is None


def test_explicit_scope_still_wins_and_keeps_chat_session(service):
    svc, _ = service
    provider = _provider(svc)
    provider.initialize(
        "chat-1",
        tenant_id="t1",
        profile_id="p1",
        collection_id="c1",
        agent_id="a1",
        **_hermes_kwargs("octacon"),
    )
    assert provider._scope == Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="chat-1"
    )


# -- profile-wide recall ---------------------------------------------------

def test_profile_remembers_across_chats(service):
    svc, _ = service
    first = _provider(svc)
    first.initialize("chat-1", **_hermes_kwargs("octacon"))
    first.sync_turn("ZEBRA-DEPLOY-WINDOW is Thursdays", "", session_id="chat-1")

    later = _provider(svc)
    later.initialize("chat-2", **_hermes_kwargs("octacon"))
    context = later.prefetch("ZEBRA-DEPLOY-WINDOW", session_id="chat-2", derive=True)

    assert "ZEBRA-DEPLOY-WINDOW" in context


def test_other_profile_never_sees_the_memory(service):
    svc, _ = service
    octacon = _provider(svc)
    octacon.initialize("chat-1", **_hermes_kwargs("octacon"))
    octacon.sync_turn("OCTACON-ONLY-SECRET marker", "", session_id="chat-1")

    remii = _provider(svc)
    remii.initialize("chat-9", **_hermes_kwargs("remii"))
    context = remii.prefetch("OCTACON-ONLY-SECRET", session_id="chat-9", derive=True)

    assert "OCTACON-ONLY-SECRET" not in context


def test_profile_bootstrap_reference_is_still_included(service):
    svc, _ = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    svc.ingest_observation(
        Scope(
            tenant_id="kensei",
            profile_id="octacon",
            collection_id="bootstrap",
            agent_id="octacon",
            session_id="shared",
        ),
        content="BOOTSTRAP-REFERENCE-FACT",
        source={"uri": "test://bootstrap"},
    )
    context = provider.prefetch("BOOTSTRAP-REFERENCE-FACT", session_id="chat-1", derive=True)
    assert "BOOTSTRAP-REFERENCE-FACT" in context
    assert "Profile bootstrap reference" in context


def test_chat_id_is_kept_as_provenance(service):
    svc, repo = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    provider.sync_turn("PROVENANCE-MARKER", "", session_id="chat-1")
    observations = repo.list_observations(provider._scope)
    assert len(observations) == 1
    assert observations[0].source["session_id"] == "chat-1"
    assert observations[0].source["platform"] == "discord"


def test_sync_from_a_different_chat_is_rejected(service):
    svc, _ = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    with pytest.raises(ScopeNotEstablishedError):
        provider.sync_turn("stray turn", "", session_id="chat-other")
    with pytest.raises(ScopeNotEstablishedError):
        provider.prefetch("stray", session_id="chat-other")


def test_session_switch_keeps_profile_scope_and_tracks_new_chat(service):
    svc, repo = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    scope_before = provider._scope
    provider.on_session_switch("chat-2", parent_session_id="chat-1", reset=True)
    assert provider._scope == scope_before
    provider.sync_turn("AFTER-SWITCH-MARKER", "", session_id="chat-2")
    observations = repo.list_observations(provider._scope)
    assert [obs.source["session_id"] for obs in observations] == ["chat-2"]


# -- non-primary contexts never write --------------------------------------

@pytest.mark.parametrize("context", ["cron", "subagent", "flush"])
def test_non_primary_context_does_not_write(service, context):
    svc, repo = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon", agent_context=context))
    provider.sync_turn("CRON-NOISE", "assistant reply", session_id="chat-1")
    provider.on_delegation("task", "result", child_session_id="child-1")
    assert repo.list_observations(provider._scope) == []


def test_non_primary_context_can_still_read(service):
    svc, _ = service
    writer = _provider(svc)
    writer.initialize("chat-1", **_hermes_kwargs("octacon"))
    writer.sync_turn("READABLE-BY-CRON fact", "", session_id="chat-1")

    cron = _provider(svc)
    cron.initialize("cron-1", **_hermes_kwargs("octacon", agent_context="cron", platform="cron"))
    assert "READABLE-BY-CRON" in cron.prefetch("READABLE-BY-CRON", session_id="cron-1", derive=True)


# -- tools stay usable -----------------------------------------------------

def test_system_prompt_names_the_active_scope_for_tool_calls(service):
    svc, _ = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    block = provider.system_prompt_block()
    for dimension, value in provider._scope.model_dump().items():
        assert f"{dimension}={value}" in block


def test_tool_call_with_the_named_scope_succeeds(service):
    svc, _ = service
    provider = _provider(svc)
    provider.initialize("chat-1", **_hermes_kwargs("octacon"))
    args = {**provider._scope.model_dump(), "content": "tool-ingested fact"}
    result = json.loads(provider.handle_tool_call("severian_ingest", args))
    assert "observation_id" in result


# -- default shared store --------------------------------------------------

def _construct_zero_arg(monkeypatch, hermes_home: Path) -> SeverianMemoryProvider:
    monkeypatch.delenv("SEVERIAN_STORAGE", raising=False)
    monkeypatch.delenv("SEVERIAN_DSN", raising=False)
    monkeypatch.delenv("SEVERIAN_EMBEDDING", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return SeverianMemoryProvider()


def test_unset_storage_uses_the_shared_store_of_a_profile_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    profile_home = root / "profiles" / "octacon"
    profile_home.mkdir(parents=True)
    provider = _construct_zero_arg(monkeypatch, profile_home)
    try:
        assert (root / "severian" / "severian.db").is_file()
        assert not (profile_home / "severian").exists()
    finally:
        provider.shutdown()


def test_unset_storage_uses_the_shared_store_of_the_root_home(tmp_path, monkeypatch):
    root = tmp_path / "hermes"
    root.mkdir()
    provider = _construct_zero_arg(monkeypatch, root)
    try:
        assert (root / "severian" / "severian.db").is_file()
    finally:
        provider.shutdown()
