"""Tests for the Python SDK.

The SDK is a thin transport layer over the HTTP API. It must:
- never invent scope silently (scope is always an explicit argument);
- serialise requests with the exact wire shape the API expects;
- surface server errors with the response body intact.
"""
from __future__ import annotations

from typing import Any

import pytest

from severian.sdk.client import (
    IngestRequest as SDKIngestRequest,
)
from severian.sdk.client import (
    ObservationScope as SDKScope,
)
from severian.sdk.client import (
    SearchRequest as SDKSearchRequest,
)
from severian.sdk.client import SeverianClient


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []

    def request(
        self, method: str, url: str, *, json: Any = None, params: Any = None
    ) -> Any:
        self.calls.append((method, url, json))
        return _FakeResponse(200, {"observation_id": "x"})


class RecordingTransport:
    """Records calls and returns a configurable response."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[tuple[str, str, Any, Any]] = []

    def request(
        self, method: str, url: str, *, json: Any = None, params: Any = None
    ) -> Any:
        self.calls.append((method, url, json, params))
        return self._response


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _scope_dict() -> dict[str, str]:
    return {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }


def test_ingest_request_serialises_scope_and_content():
    req = SDKIngestRequest(
        scope=SDKScope(**_scope_dict()),
        content="Hello",
        source={"uri": "note://1"},
    )
    payload = req.to_payload()
    assert payload["scope"] == _scope_dict()
    assert payload["content"] == "Hello"
    assert payload["source"] == {"uri": "note://1"}


def test_search_request_serialises_with_derive_flag():
    req = SDKSearchRequest(
        scope=SDKScope(**_scope_dict()),
        query="Ada",
        limit=5,
        derive=True,
    )
    payload = req.to_payload()
    assert payload["scope"] == _scope_dict()
    assert payload["query"] == "Ada"
    assert payload["limit"] == 5
    assert payload["derive"] is True


def test_client_ingest_observation_sends_post_with_payload():
    body = {
        "observation_id": "obs-1",
        "content": "Hello",
        "content_hash": "h",
        "scope": _scope_dict(),
        "source": {"uri": "note://1"},
        "job_id": "job-1",
        "job_kind": "derive",
    }
    transport = RecordingTransport(_FakeResponse(201, body))
    client = SeverianClient(base_url="http://testserver", transport=transport)  # type: ignore[arg-type]
    result = client.ingest_observation(
        scope=SDKScope(**_scope_dict()),
        content="Hello",
        source={"uri": "note://1"},
    )
    method, url, payload, _ = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/api/v1/observations")
    assert payload["scope"] == _scope_dict()
    assert payload["content"] == "Hello"
    assert result.observation_id == "obs-1"
    assert result.job_kind == "derive"


def test_client_get_observation_sends_scope_as_query_params():
    body = {
        "observation_id": "obs-1",
        "content": "x",
        "content_hash": "h",
        "scope": _scope_dict(),
        "source": {},
        "created_at": "2026-07-31T00:00:00+00:00",
    }
    transport = RecordingTransport(_FakeResponse(200, body))
    client = SeverianClient(base_url="http://testserver", transport=transport)  # type: ignore[arg-type]
    result = client.get_observation(observation_id="obs-1", scope=SDKScope(**_scope_dict()))
    method, url, _payload, params = transport.calls[0]
    assert method == "GET"
    assert url.endswith("/api/v1/observations/obs-1")
    assert params == _scope_dict()
    assert result.observation_id == "obs-1"


def test_client_search_sends_post_with_derive_flag():
    transport = RecordingTransport(_FakeResponse(200, {"hits": []}))
    client = SeverianClient(base_url="http://testserver", transport=transport)  # type: ignore[arg-type]
    client.search(
        scope=SDKScope(**_scope_dict()),
        query="Ada",
        limit=5,
        derive=True,
    )
    method, url, payload, _params = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/api/v1/search")
    assert payload["derive"] is True
    assert payload["scope"] == _scope_dict()


def test_client_requires_explicit_scope():
    transport = RecordingTransport(_FakeResponse(200, {}))
    client = SeverianClient(base_url="http://testserver", transport=transport)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scope"):
        client.ingest_observation(scope=None, content="x", source={})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scope"):
        client.search(scope=None, query="x")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scope"):
        client.get_observation(observation_id="x", scope=None)  # type: ignore[arg-type]


def test_client_propagates_server_errors_with_safe_detail():
    transport = RecordingTransport(_FakeResponse(500, {"detail": "internal error"}))
    client = SeverianClient(base_url="http://testserver", transport=transport)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="500"):
        client.ingest_observation(scope=SDKScope(**_scope_dict()), content="x", source={})
