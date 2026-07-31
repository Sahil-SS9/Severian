"""Typed httpx client for the Severian HTTP API.

The SDK exposes one method per documented endpoint and refuses to
silently invent scope. Tests inject a transport to assert the exact
wire format; the default transport is :mod:`httpx`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from severian.api.schemas import (
    IngestResponse as APISchemaIngestResponse,
)
from severian.api.schemas import (
    ObservationResponse as APISchemaObservation,
)
from severian.api.schemas import (
    ScopeModel as APISchemaScope,
)
from severian.api.schemas import (
    SearchResponse as APISchemaSearchResponse,
)


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> Any: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ObservationScope:
    """Mandatory scope for every SDK call.

    The SDK refuses to dispatch a request without an explicit scope
    on every call. The server enforces the same constraint and the
    wire model is the same: ``tenant_id``, ``profile_id``,
    ``collection_id``, ``agent_id``, ``session_id``.
    """

    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str

    def to_payload(self) -> dict[str, str]:
        return {
            "tenant_id": self.tenant_id,
            "profile_id": self.profile_id,
            "collection_id": self.collection_id,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
        }

    def __post_init__(self) -> None:
        for field in self.to_payload().values():
            if not field or not field.strip():
                raise ValueError("scope dimensions must not be empty")


@dataclass(frozen=True)
class IngestRequest:
    scope: ObservationScope
    content: str
    source: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_payload(),
            "content": self.content,
            "source": dict(self.source),
        }


@dataclass(frozen=True)
class SearchRequest:
    scope: ObservationScope
    query: str
    limit: int = 20
    derive: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_payload(),
            "query": self.query,
            "limit": self.limit,
            "derive": self.derive,
        }


class _HTTPXTransport:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def request(
        self, method: str, url: str, *, json: Any = None, params: Any = None
    ) -> Any:
        response = self._client.request(method, url, json=json, params=params)
        # ``raise_for_status`` is invoked by the SDK; tests can mock
        # this path by injecting a custom transport.
        response.raise_for_status()
        return response

    def close(self) -> None:
        self._client.close()


class SeverianError(RuntimeError):
    """Raised when the server returns a non-2xx response."""

    def __init__(self, status_code: int, body: Any) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"server returned {status_code}: {body!r}")


class SeverianClient:
    """Typed client for the Severian HTTP API."""

    def __init__(
        self,
        *,
        base_url: str,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport: Transport = transport or _HTTPXTransport(base_url, timeout=timeout)
        self._owns_transport = transport is None
        # Warm the schemas so import-time errors surface early.
        _ = APISchemaScope.model_json_schema()

    def close(self) -> None:
        if self._owns_transport:
            close = getattr(self._transport, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> SeverianClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _require_scope(scope: ObservationScope | None) -> ObservationScope:
        if scope is None:
            raise ValueError("scope is required: SDK will not invent scope")
        return scope

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Any = None,
    ) -> Any:
        url = f"{self._base_url}{path}"
        response = self._transport.request(method, url, json=json, params=params)
        if getattr(response, "status_code", 200) >= 400:
            body: Any
            try:
                body = response.json()
            except Exception:
                body = getattr(response, "text", "")
            raise SeverianError(response.status_code, body)
        return response

    # -- endpoints ------------------------------------------------------

    def ingest_observation(
        self,
        *,
        scope: ObservationScope,
        content: str,
        source: Mapping[str, Any] | None = None,
    ) -> APISchemaIngestResponse:
        self._require_scope(scope)
        request = IngestRequest(scope=scope, content=content, source=source or {})
        response = self._request(
            "POST", "/api/v1/observations", json=request.to_payload()
        )
        return APISchemaIngestResponse.model_validate(response.json())

    def get_observation(
        self, *, observation_id: str, scope: ObservationScope
    ) -> APISchemaObservation:
        self._require_scope(scope)
        response = self._request(
            "GET",
            f"/api/v1/observations/{observation_id}",
            params=scope.to_payload(),
        )
        return APISchemaObservation.model_validate(response.json())

    def search(
        self,
        *,
        scope: ObservationScope,
        query: str,
        limit: int = 20,
        derive: bool = False,
    ) -> APISchemaSearchResponse:
        self._require_scope(scope)
        request = SearchRequest(scope=scope, query=query, limit=limit, derive=derive)
        response = self._request("POST", "/api/v1/search", json=request.to_payload())
        return APISchemaSearchResponse.model_validate(response.json())


__all__ = [
    "IngestRequest",
    "ObservationScope",
    "SearchRequest",
    "SeverianClient",
    "SeverianError",
    "Transport",
]
