"""Tests for the MCP JSON-RPC 2.0 server adapter.

Covers protocol handshake, tools/list, tools/call with explicit scope,
cross-scope rejection, malformed input, and stdio runner.
"""
from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.domain.models import Collection
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex
from severian.mcp.server import (
    ERR_INVALID_REQUEST,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE_ERROR,
    PROTOCOL_VERSION,
    SeverianMCPServer,
)


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


@pytest.fixture
def server(tmp_path: Path):
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
    srv = SeverianMCPServer(service=service, gateway=FakeGateway())
    # P1-4: MCP requires the initialize handshake before tools/list or
    # tools/call. The fixture performs it so tool tests exercise real
    # protocol state (a separate test covers the rejection path).
    srv.handle_message(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    )
    srv.handle_message(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    yield srv
    fts.close()
    vector.close()
    repo.close()


# -- protocol handshake ---------------------------------------------------

def test_initialize_returns_protocol_and_capabilities(server):
    response = server.handle_message(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    )
    assert response is not None
    assert response["jsonrpc"] == "2.0"
    assert response["id"] == 1
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in result["capabilities"]


def test_initialized_notification_returns_nothing(server):
    response = server.handle_message(
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    )
    assert response is None


def test_ping_returns_empty_result(server):
    response = server.handle_message(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}))
    assert response is not None
    assert response["id"] == 7
    assert response["result"] == {}


# -- tools/list -----------------------------------------------------------

def test_tools_list_before_initialize_rejected(tmp_path):
    # P1-4 repro: _initialized was set but never enforced; a client
    # could call tools/list and tools/call without the handshake.
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
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
    srv = SeverianMCPServer(service=service, gateway=FakeGateway())
    response = srv.handle_message(
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    )
    assert response is not None
    assert "error" in response
    assert response["error"]["code"] == -32002  # ERR_SERVER_NOT_INITIALIZED
    fts.close()
    vector.close()
    repo.close()


def test_tools_list_advertises_three_tools(server):
    response = server.handle_message(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    tools = response["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "severian_ingest_observation",
        "severian_search",
        "severian_get_observation",
    }
    for tool in tools:
        assert "inputSchema" in tool
        # Scope dims must be required in each schema.
        required = set(tool["inputSchema"].get("required", []))
        for dim in ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id"):
            assert dim in required, f"{dim} must be required in {tool['name']}"


# -- tools/call: ingest --------------------------------------------------

def test_ingest_tool_round_trip(server):
    call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {**_scope_args(), "content": "Ada founded a company", "source": {"uri": "note://1"}},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["jsonrpc"] == "2.0"
    content = response["result"]["content"]
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["observation_id"]
    assert payload["content"] == "Ada founded a company"
    assert payload["job_kind"] == "derive"
    assert response["result"]["isError"] is False


def test_ingest_tool_rejects_missing_scope(server):
    call = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {"content": "no scope"},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True
    text = json.loads(response["result"]["content"][0]["text"])
    assert "invalid arguments" in text["error"]


def test_invalid_scope_is_rejected_before_service_access(server, monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("service must not be touched")

    monkeypatch.setattr(server._service, "ingest_observation", fail)
    call = {
        "jsonrpc": "2.0",
        "id": 401,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {"content": "blocked before service"},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True


def test_ingest_tool_rejects_blank_scope(server):
    call = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {**_scope_args(tenant_id="   "), "content": "x"},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True


def test_ingest_tool_rejects_empty_content(server):
    call = {
        "jsonrpc": "2.0",
        "id": 6,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {**_scope_args(), "content": "  \n  "},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True


# -- tools/call: search ---------------------------------------------------

def test_search_tool_returns_hits(server):
    # Ingest first.
    ingest_call = {
        "jsonrpc": "2.0",
        "id": 10,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {**_scope_args(), "content": "Ada founded a company"},
        },
    }
    server.handle_message(json.dumps(ingest_call))
    # Search with derive.
    search_call = {
        "jsonrpc": "2.0",
        "id": 11,
        "method": "tools/call",
        "params": {
            "name": "severian_search",
            "arguments": {**_scope_args(), "query": "Ada", "derive": True, "limit": 5},
        },
    }
    response = server.handle_message(json.dumps(search_call))
    payload = json.loads(response["result"]["content"][0]["text"])
    assert len(payload["hits"]) >= 1
    assert payload["hits"][0]["source_observation_ids"]


def test_search_tool_rejects_missing_scope(server):
    call = {
        "jsonrpc": "2.0",
        "id": 12,
        "method": "tools/call",
        "params": {
            "name": "severian_search",
            "arguments": {"query": "Ada"},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True


def test_search_derive_without_gateway_returns_error(tmp_path: Path):
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
    srv = SeverianMCPServer(service=service, gateway=None)
    # P1-4: complete the initialize handshake before tools/call.
    srv.handle_message(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}}))
    call = {
        "jsonrpc": "2.0",
        "id": 13,
        "method": "tools/call",
        "params": {
            "name": "severian_search",
            "arguments": {**_scope_args(), "query": "Ada", "derive": True},
        },
    }
    response = srv.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "gateway" in payload["error"]
    fts.close()
    vector.close()
    repo.close()


# -- tools/call: get_observation ------------------------------------------

def test_get_observation_tool_round_trip(server):
    ingest_call = {
        "jsonrpc": "2.0",
        "id": 20,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {**_scope_args(), "content": "Hello world"},
        },
    }
    ingest_response = server.handle_message(json.dumps(ingest_call))
    obs_id = json.loads(ingest_response["result"]["content"][0]["text"])["observation_id"]
    get_call = {
        "jsonrpc": "2.0",
        "id": 21,
        "method": "tools/call",
        "params": {
            "name": "severian_get_observation",
            "arguments": {**_scope_args(), "observation_id": obs_id},
        },
    }
    response = server.handle_message(json.dumps(get_call))
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["observation_id"] == obs_id
    assert payload["content"] == "Hello world"


def test_get_observation_cross_scope_returns_error(server):
    ingest_call = {
        "jsonrpc": "2.0",
        "id": 30,
        "method": "tools/call",
        "params": {
            "name": "severian_ingest_observation",
            "arguments": {**_scope_args(), "content": "secret"},
        },
    }
    ingest_response = server.handle_message(json.dumps(ingest_call))
    obs_id = json.loads(ingest_response["result"]["content"][0]["text"])["observation_id"]
    # Cross-scope: different profile.
    get_call = {
        "jsonrpc": "2.0",
        "id": 31,
        "method": "tools/call",
        "params": {
            "name": "severian_get_observation",
            "arguments": {**_scope_args(profile_id="p2"), "observation_id": obs_id},
        },
    }
    response = server.handle_message(json.dumps(get_call))
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "not found" in payload["error"]


# -- error handling -------------------------------------------------------

def test_unknown_method_returns_method_not_found(server):
    response = server.handle_message(
        json.dumps({"jsonrpc": "2.0", "id": 40, "method": "nonexistent/method"})
    )
    assert response["error"]["code"] == ERR_METHOD_NOT_FOUND


def test_malformed_json_returns_parse_error(server):
    response = server.handle_message("not valid json {{{")
    assert response["error"]["code"] == ERR_PARSE_ERROR
    assert response["id"] is None


def test_non_object_request_returns_invalid_request(server):
    response = server.handle_message(json.dumps([1, 2, 3]))
    assert response["error"]["code"] == ERR_INVALID_REQUEST


def test_missing_method_returns_invalid_request(server):
    response = server.handle_message(json.dumps({"jsonrpc": "2.0", "id": 41}))
    assert response["error"]["code"] == ERR_INVALID_REQUEST


def test_wrong_jsonrpc_version_returns_invalid_request(server):
    response = server.handle_message(
        json.dumps({"jsonrpc": "1.0", "id": 42, "method": "ping"})
    )
    assert response["error"]["code"] == ERR_INVALID_REQUEST


def test_ping_notification_returns_no_response(server):
    response = server.handle_message(json.dumps({"jsonrpc": "2.0", "method": "ping"}))
    assert response is None


def test_unknown_tool_returns_is_error(server):
    call = {
        "jsonrpc": "2.0",
        "id": 50,
        "method": "tools/call",
        "params": {"name": "nonexistent_tool", "arguments": {}},
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "unknown tool" in payload["error"]


def test_tools_call_missing_name_returns_is_error(server):
    call = {
        "jsonrpc": "2.0",
        "id": 51,
        "method": "tools/call",
        "params": {"arguments": {}},
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True


def test_tools_call_non_dict_args_returns_is_error(server):
    call = {
        "jsonrpc": "2.0",
        "id": 52,
        "method": "tools/call",
        "params": {"name": "severian_search", "arguments": "not an object"},
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True


def test_no_exception_details_leak_on_stdout(server):
    # Inject a tool call that triggers a service ValueError; ensure the
    # error text never contains a traceback or internal class name.
    call = {
        "jsonrpc": "2.0",
        "id": 60,
        "method": "tools/call",
        "params": {
            "name": "severian_search",
            "arguments": {**_scope_args(), "query": "Ada", "derive": True, "limit": 0},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True
    text = response["result"]["content"][0]["text"]
    assert "Traceback" not in text
    assert ".py" not in text


def test_service_value_error_is_replaced_with_static_error(server, monkeypatch):
    def explode(*args, **kwargs):
        raise ValueError("/private/canonical.db leaked")

    monkeypatch.setattr(server._service, "search", explode)
    call = {
        "jsonrpc": "2.0",
        "id": 61,
        "method": "tools/call",
        "params": {
            "name": "severian_search",
            "arguments": {**_scope_args(), "query": "Ada"},
        },
    }
    response = server.handle_message(json.dumps(call))
    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is True
    assert "/private/canonical.db" not in text
    assert json.loads(text)["error"] == "invalid request"


def test_search_rejects_whitespace_query(server):
    call = {
        "jsonrpc": "2.0",
        "id": 62,
        "method": "tools/call",
        "params": {
            "name": "severian_search",
            "arguments": {**_scope_args(), "query": "   "},
        },
    }
    response = server.handle_message(json.dumps(call))
    assert response["result"]["isError"] is True


# -- stdio runner ---------------------------------------------------------

def test_stdio_runner_emits_protocol_json_only(server):
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"}),
        "",  # blank line should be skipped
    ]
    input_stream = io.StringIO("\n".join(lines) + "\n")
    output_stream = io.StringIO()
    server.run_stdio(input_stream=input_stream, output_stream=output_stream)
    output = output_stream.getvalue().strip().splitlines()
    # notifications produce no output; blank lines skipped.
    assert len(output) == 2
    for line in output:
        parsed = json.loads(line)
        assert parsed["jsonrpc"] == "2.0"
        assert "id" in parsed


def test_stdio_runner_parse_error_emits_error(server):
    input_stream = io.StringIO("garbage\n")
    output_stream = io.StringIO()
    server.run_stdio(input_stream=input_stream, output_stream=output_stream)
    output = output_stream.getvalue().strip()
    parsed = json.loads(output)
    assert parsed["error"]["code"] == ERR_PARSE_ERROR
