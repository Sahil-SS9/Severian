"""Canonical MCP (Model Context Protocol) server adapter.

This module implements a minimal but spec-aligned MCP server over
JSON-RPC 2.0 using only the standard library and Pydantic. It does
not depend on the optional ``mcp`` SDK package. The server speaks the
stdio transport: one JSON-RPC message per line on stdin/stdout.

Protocol surface implemented:

- ``initialize`` / ``notifications/initialized`` handshake.
- ``ping``.
- ``tools/list`` — advertises the three Severian tools.
- ``tools/call`` — dispatches a named tool with explicit scope.
- Unknown methods, malformed JSON and missing fields produce the
  appropriate JSON-RPC error. Exception details are never leaked:
  error messages are static, safe strings.

Tool results are MCP ``content`` blocks of type ``text`` carrying a
safe JSON payload. Domain/client errors set ``isError: true`` with a
text block; protocol/transport errors use JSON-RPC error objects.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from severian.application.derivation import DerivationGateway
from severian.application.service import MemoryService
from severian.domain.errors import NotFoundError
from severian.domain.models import Scope

# -- protocol constants ---------------------------------------------------

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "severian-mcp"
SERVER_VERSION = "0.1.0"

# JSON-RPC error codes (per spec).
ERR_PARSE_ERROR = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL_ERROR = -32603


# -- scope validation -----------------------------------------------------

_SCOPE_DIMS = ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")


class MCPScope(BaseModel):
    """Five-dimensional scope, mandatory and non-empty on every tool call."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str

    @field_validator(*_SCOPE_DIMS)
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("scope dimensions must not be empty")
        return value

    def to_domain(self) -> Scope:
        return Scope(
            tenant_id=self.tenant_id,
            profile_id=self.profile_id,
            collection_id=self.collection_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
        )


# -- tool argument models --------------------------------------------------

class IngestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str
    content: str = Field(min_length=1, max_length=1_000_000)
    source: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content")
    @classmethod
    def _content_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @field_validator(*_SCOPE_DIMS)
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("scope dimensions must not be empty")
        return value

    def scope(self) -> Scope:
        return Scope(
            tenant_id=self.tenant_id,
            profile_id=self.profile_id,
            collection_id=self.collection_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
        )


class SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str
    query: str = Field(min_length=1, max_length=10_000)
    limit: int = Field(default=20, ge=1, le=200)
    derive: bool = False

    @field_validator("query")
    @classmethod
    def _query_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @field_validator(*_SCOPE_DIMS)
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("scope dimensions must not be empty")
        return value

    def scope(self) -> Scope:
        return Scope(
            tenant_id=self.tenant_id,
            profile_id=self.profile_id,
            collection_id=self.collection_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
        )


class GetObservationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str
    observation_id: str = Field(min_length=1)

    @field_validator("observation_id")
    @classmethod
    def _observation_id_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("observation_id must not be blank")
        return value

    @field_validator(*_SCOPE_DIMS)
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("scope dimensions must not be empty")
        return value

    def scope(self) -> Scope:
        return Scope(
            tenant_id=self.tenant_id,
            profile_id=self.profile_id,
            collection_id=self.collection_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
        )


# -- tool definitions ------------------------------------------------------

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


_TOOLS: tuple[ToolDefinition, ...] = (
    ToolDefinition(
        name="severian_ingest_observation",
        description=(
            "Ingest an observation into the canonical Severian memory store "
            "for the given five-dimensional scope. Returns the observation id "
            "and metadata. All scope dimensions are mandatory."
        ),
        input_schema=IngestArgs.model_json_schema(),
    ),
    ToolDefinition(
        name="severian_search",
        description=(
            "Search derived memories within a scope using hybrid lexical + "
            "vector retrieval. Optionally runs derivation before searching. "
            "All scope dimensions are mandatory."
        ),
        input_schema=SearchArgs.model_json_schema(),
    ),
    ToolDefinition(
        name="severian_get_observation",
        description=(
            "Fetch a single observation by id within a scope. Cross-scope "
            "lookups return an error without revealing existence. All scope "
            "dimensions are mandatory."
        ),
        input_schema=GetObservationArgs.model_json_schema(),
    ),
)


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.input_schema,
        }
        for tool in _TOOLS
    ]


# -- JSON-RPC helpers ------------------------------------------------------

def _error(code: int, message: str, request_id: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _result(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _text_content(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Build an MCP ``tools/call`` result envelope."""
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


def _safe_json(payload: Any) -> str:
    """Serialise to compact JSON with safe defaults (no NaN/Inf leak)."""
    return json.dumps(payload, sort_keys=True, default=str, allow_nan=False)


# -- the server ------------------------------------------------------------

class SeverianMCPServer:
    """Canonical MCP adapter over JSON-RPC 2.0 / stdio.

    The server wraps a :class:`MemoryService` and optionally a
    :class:`DerivationGateway`. It never writes the repository or
    indexes directly; all operations go through the service.

    Lifecycle: construct once, then call :meth:`handle_message` for each
    inbound JSON-RPC line (or :meth:`run_stdio` for a blocking loop).
    """

    def __init__(
        self,
        service: MemoryService,
        gateway: DerivationGateway | None = None,
    ) -> None:
        self._service = service
        self._gateway = gateway
        self._initialized = False

    # -- public dispatch --------------------------------------------------

    def handle_message(self, raw: str) -> dict[str, Any] | None:
        """Process one JSON-RPC message line.

        Returns the response object to emit, or ``None`` for
        notifications (which carry no ``id`` and expect no response).
        Malformed input yields a JSON-RPC error with ``id: null``.
        """
        # 1. Parse the raw line.
        try:
            payload: Any = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return _error(ERR_PARSE_ERROR, "parse error", None)

        if not isinstance(payload, dict):
            return _error(ERR_INVALID_REQUEST, "invalid request: not an object", None)

        if payload.get("jsonrpc") != "2.0":
            return _error(ERR_INVALID_REQUEST, "invalid request: jsonrpc must be 2.0", payload.get("id"))

        method = payload.get("method")
        request_id = payload.get("id")
        params = payload.get("params", {})

        if not isinstance(method, str) or not method:
            return _error(ERR_INVALID_REQUEST, "invalid request: missing method", request_id)

        is_notification = "id" not in payload

        # 2. Dispatch.
        try:
            return self._dispatch(method, params, request_id, is_notification)
        except _ProtocolError as exc:
            return _error(exc.code, exc.message, request_id)
        except Exception:  # pragma: no cover - defence-in-depth
            # Never leak exception details over the wire.
            return _error(ERR_INTERNAL_ERROR, "internal error", request_id)

    # -- method handlers --------------------------------------------------

    def _dispatch(
        self,
        method: str,
        params: Any,
        request_id: Any,
        is_notification: bool,
    ) -> dict[str, Any] | None:
        if method == "initialize":
            response = self._handle_initialize(params, request_id)
            return None if is_notification else response
        if method == "notifications/initialized":
            self._initialized = True
            return None
        if method == "ping":
            response = self._handle_ping(request_id)
            return None if is_notification else response
        if method == "tools/list":
            response = self._handle_tools_list(request_id)
            return None if is_notification else response
        if method == "tools/call":
            if is_notification:
                return None
            return self._handle_tools_call(params, request_id)
        raise _ProtocolError(ERR_METHOD_NOT_FOUND, f"method not found: {method}")

    def _handle_initialize(self, params: Any, request_id: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _ProtocolError(ERR_INVALID_PARAMS, "initialize params must be an object")
        return _result(
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
            request_id,
        )

    def _handle_ping(self, request_id: Any) -> dict[str, Any]:
        return _result({}, request_id)

    def _handle_tools_list(self, request_id: Any) -> dict[str, Any]:
        return _result({"tools": _tool_list()}, request_id)

    def _handle_tools_call(self, params: Any, request_id: Any) -> dict[str, Any]:
        if not isinstance(params, dict):
            raise _ProtocolError(ERR_INVALID_PARAMS, "tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not name:
            return _result(
                _text_content(_safe_json({"error": "missing tool name"}), is_error=True), request_id
            )
        if not isinstance(arguments, dict):
            return _result(
                _text_content(_safe_json({"error": "tool arguments must be an object"}), is_error=True),
                request_id,
            )

        try:
            if name == "severian_ingest_observation":
                return self._call_ingest(arguments, request_id)
            if name == "severian_search":
                return self._call_search(arguments, request_id)
            if name == "severian_get_observation":
                return self._call_get_observation(arguments, request_id)
            return _result(_text_content(_safe_json({"error": "unknown tool"}), is_error=True), request_id)
        except ValidationError as exc:
            # Safe, structured validation error — strip Pydantic URLs
            # (which contain ".py") and only keep loc/msg/type.
            safe_details = [
                {"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")}
                for err in exc.errors()
            ]
            return _result(
                _text_content(_safe_json({"error": "invalid arguments", "details": safe_details}), is_error=True),
                request_id,
            )
        except _ScopeError as exc:
            return _result(
                _text_content(_safe_json({"error": "scope error", "detail": str(exc)}), is_error=True),
                request_id,
            )
        except NotFoundError:
            return _result(
                _text_content(_safe_json({"error": "not found in scope"}), is_error=True),
                request_id,
            )
        except ValueError:
            return _result(
                _text_content(_safe_json({"error": "invalid request"}), is_error=True),
                request_id,
            )

    # -- tool implementations --------------------------------------------

    def _call_ingest(self, arguments: dict[str, Any], request_id: Any) -> dict[str, Any]:
        args = IngestArgs.model_validate(arguments)
        scope = args.scope()
        view = self._service.ingest_observation(scope, content=args.content, source=args.source)
        payload = {
            "observation_id": view.observation_id,
            "content": view.content,
            "content_hash": view.content_hash,
            "scope": view.scope.model_dump(),
            "source": dict(view.source),
            "job_id": view.job_id,
            "job_kind": view.job_kind,
        }
        return _result(_text_content(_safe_json(payload)), request_id)

    def _call_search(self, arguments: dict[str, Any], request_id: Any) -> dict[str, Any]:
        args = SearchArgs.model_validate(arguments)
        scope = args.scope()
        if args.derive and self._gateway is None:
            return _result(
                _text_content(_safe_json({"error": "derive requested but no gateway configured"}), is_error=True),
                request_id,
            )
        hits = self._service.search(
            scope,
            query=args.query,
            limit=args.limit,
            derive=args.derive,
            gateway=self._gateway,
        )
        payload = {
            "hits": [
                {
                    "memory_id": hit.memory_id,
                    "content": hit.content,
                    "source_observation_ids": list(hit.source_observation_ids),
                    "lexical_score": hit.lexical_score,
                    "vector_score": hit.vector_score,
                    "fused_score": hit.fused_score,
                    "scope": hit.scope.model_dump(),
                }
                for hit in hits
            ]
        }
        return _result(_text_content(_safe_json(payload)), request_id)

    def _call_get_observation(self, arguments: dict[str, Any], request_id: Any) -> dict[str, Any]:
        args = GetObservationArgs.model_validate(arguments)
        scope = args.scope()
        observation = self._service.get_observation(scope, args.observation_id)
        payload = {
            "observation_id": observation.id,
            "content": observation.content,
            "content_hash": observation.content_hash,
            "scope": observation.scope.model_dump(),
            "source": dict(observation.source),
            "created_at": observation.created_at.isoformat(),
        }
        return _result(_text_content(_safe_json(payload)), request_id)

    # -- stdio runner -----------------------------------------------------

    def run_stdio(self, *, input_stream: Any = None, output_stream: Any = None) -> None:
        """Blocking stdio runner.

        Reads JSON-RPC messages line-by-line from stdin and writes one
        response per line to stdout. Only protocol JSON is written to
        stdout; diagnostics (if any) go to stderr. Exceptions are
        caught and surfaced as JSON-RPC internal errors — never as
        tracebacks on stdout.
        """
        instream = input_stream if input_stream is not None else sys.stdin
        outstream = output_stream if output_stream is not None else sys.stdout
        for line in instream:
            line = line.strip()
            if not line:
                continue
            response = self.handle_message(line)
            if response is not None:
                outstream.write(json.dumps(response, allow_nan=False) + "\n")
                outstream.flush()


class _ProtocolError(Exception):
    """Internal control-flow error mapped to a JSON-RPC error code."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class _ScopeError(Exception):
    """Raised when scope cannot be established for a tool call."""


__all__ = [
    "GetObservationArgs",
    "IngestArgs",
    "MCPScope",
    "PROTOCOL_VERSION",
    "SearchArgs",
    "SeverianMCPServer",
    "SERVER_NAME",
    "SERVER_VERSION",
]
