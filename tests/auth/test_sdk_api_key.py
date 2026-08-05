"""Tests for SDK API-key credential support (SEC-01).

RED-first: ``SeverianClient`` must accept ``api_key`` and send
``Authorization: Bearer <key>`` on every request.
"""
from __future__ import annotations

from typing import Any

from severian.sdk.client import SeverianClient


class RecordingTransport:
    def __init__(self, response: Any) -> None:
        self._response = response
        self.calls: list[tuple[str, str, Any, Any, dict[str, str]]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append((method, url, json, params, headers or {}))
        return self._response

    def close(self) -> None: ...


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> dict[str, Any]:
        return self._body


def _scope_dict() -> dict[str, str]:
    return {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }


def _ingest_body() -> dict[str, Any]:
    return {
        "observation_id": "obs-1",
        "content": "Hello",
        "content_hash": "h",
        "scope": _scope_dict(),
        "source": {},
        "job_id": "job-1",
        "job_kind": "derive",
    }


def _obs_body() -> dict[str, Any]:
    return {
        "observation_id": "obs-1",
        "content": "x",
        "content_hash": "h",
        "scope": _scope_dict(),
        "source": {},
        "created_at": "2026-01-01T00:00:00+00:00",
    }


# -- api_key is accepted ---------------------------------------------------

def test_client_accepts_api_key():
    client = SeverianClient(base_url="http://test", api_key="my-secret")
    assert client._api_key == "my-secret"
    client.close()


def test_client_without_api_key_has_none():
    client = SeverianClient(base_url="http://test")
    assert client._api_key is None
    client.close()


# -- Authorization header on every request --------------------------------

def test_ingest_sends_bearer_header():
    transport = RecordingTransport(_FakeResponse(201, _ingest_body()))
    client = SeverianClient(
        base_url="http://testserver",
        transport=transport,
        api_key="my-secret",
    )
    from severian.sdk.client import ObservationScope as SDKScope

    client.ingest_observation(
        scope=SDKScope(**_scope_dict()),
        content="Hello",
        source={},
    )
    _, _, _, _, headers = transport.calls[0]
    assert headers.get("Authorization") == "Bearer my-secret"
    client.close()


def test_search_sends_bearer_header():
    transport = RecordingTransport(_FakeResponse(200, {"hits": []}))
    client = SeverianClient(
        base_url="http://testserver",
        transport=transport,
        api_key="abc123",
    )
    from severian.sdk.client import ObservationScope as SDKScope

    client.search(
        scope=SDKScope(**_scope_dict()),
        query="Ada",
        limit=5,
        derive=True,
    )
    _, _, _, _, headers = transport.calls[0]
    assert headers.get("Authorization") == "Bearer abc123"
    client.close()


def test_get_observation_sends_bearer_header():
    transport = RecordingTransport(_FakeResponse(200, _obs_body()))
    client = SeverianClient(
        base_url="http://testserver",
        transport=transport,
        api_key="key-x",
    )
    from severian.sdk.client import ObservationScope as SDKScope

    client.get_observation(observation_id="obs-1", scope=SDKScope(**_scope_dict()))
    _, _, _, _, headers = transport.calls[0]
    assert headers.get("Authorization") == "Bearer key-x"
    client.close()


def test_no_api_key_sends_no_authorization_header():
    transport = RecordingTransport(_FakeResponse(201, _ingest_body()))
    client = SeverianClient(
        base_url="http://testserver",
        transport=transport,
    )
    from severian.sdk.client import ObservationScope as SDKScope

    client.ingest_observation(
        scope=SDKScope(**_scope_dict()),
        content="Hello",
        source={},
    )
    _, _, _, _, headers = transport.calls[0]
    assert "Authorization" not in headers
    client.close()
