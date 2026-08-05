"""Tests for MCP construct-time principal binding (SEC-01).

RED-first: the MCP server optionally accepts a principal Scope at
construction. When bound, tool args cannot switch tenant/profile —
the principal's tenant/profile override the args, and a mismatch is
rejected as a scope error (the stdio MCP transport cannot do
interactive auth; the binding is capability-scoped).
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
from severian.mcp.server import SeverianMCPServer


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _scope_args(**overrides: str) -> dict[str, str]:
    base = {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }
    base.update(overrides)
    return base


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


def _init_handshake(srv: SeverianMCPServer) -> None:
    srv.handle_message(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    )
    srv.handle_message(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))


def _tools_call(name: str, arguments: dict, request_id: int = 1) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )


# -- without principal: existing behaviour --------------------------------

def test_mcp_without_principal_uses_args_scope(tmp_path: Path):
    """No principal → tool args scope is used as-is (existing behaviour)."""
    service, repo, fts, vector = _build_service(tmp_path)
    srv = SeverianMCPServer(service=service, gateway=FakeGateway())
    _init_handshake(srv)
    response = srv.handle_message(
        _tools_call("severian_ingest_observation", {**_scope_args(), "content": "hello"})
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["observation_id"]
    fts.close()
    vector.close()
    repo.close()


# -- with principal: construct-time binding --------------------------------

@pytest.fixture
def principal_server(tmp_path: Path):
    service, repo, fts, vector = _build_service(tmp_path)
    srv = SeverianMCPServer(service=service, gateway=FakeGateway(), principal=_principal())
    _init_handshake(srv)
    yield srv
    fts.close()
    vector.close()
    repo.close()


def test_mcp_with_principal_accepts_matching_args(principal_server):
    """Args matching the principal's tenant/profile are accepted."""
    response = principal_server.handle_message(
        _tools_call("severian_ingest_observation", {**_scope_args(), "content": "ok"})
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["observation_id"]
    assert payload["scope"]["tenant_id"] == "t1"


def test_mcp_with_principal_rejects_cross_tenant_args(principal_server):
    """Args claiming a different tenant must be rejected."""
    response = principal_server.handle_message(
        _tools_call(
            "severian_ingest_observation",
            {**_scope_args(tenant_id="evil"), "content": "x"},
        )
    )
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "scope" in payload["error"].lower() or "principal" in payload["error"].lower()


def test_mcp_with_principal_rejects_cross_profile_args(principal_server):
    response = principal_server.handle_message(
        _tools_call(
            "severian_ingest_observation",
            {**_scope_args(profile_id="other"), "content": "x"},
        )
    )
    assert response["result"]["isError"] is True


def test_mcp_with_principal_search_rejects_cross_tenant(principal_server):
    response = principal_server.handle_message(
        _tools_call(
            "severian_search",
            {**_scope_args(tenant_id="evil"), "query": "x"},
        )
    )
    assert response["result"]["isError"] is True


def test_mcp_with_principal_get_observation_rejects_cross_tenant(principal_server):
    response = principal_server.handle_message(
        _tools_call(
            "severian_get_observation",
            {**_scope_args(tenant_id="evil"), "observation_id": "x"},
        )
    )
    assert response["result"]["isError"] is True


def test_mcp_principal_overrides_args_collection_is_arg_selectable(principal_server):
    """collection_id / agent_id / session_id in args are NOT security-sensitive
    and remain client-selectable even with a bound principal."""
    response = principal_server.handle_message(
        _tools_call(
            "severian_ingest_observation",
            {**_scope_args(collection_id="c2", agent_id="a2", session_id="s2"), "content": "ok"},
        )
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["observation_id"]
    # tenant/profile come from the principal; collection/agent/session from args.
    assert payload["scope"]["tenant_id"] == "t1"
    assert payload["scope"]["profile_id"] == "p1"
    assert payload["scope"]["collection_id"] == "c2"
