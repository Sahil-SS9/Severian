"""Tests for the principal resolver (SEC-01).

RED-first: the resolver must extract a Bearer token, resolve it via
the :class:`ApiKeyStore`, and return the bound principal. When auth
is disabled (no store), it returns ``None``. Fail-closed: missing /
invalid tokens raise 401.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from severian.auth.principal_resolver import PrincipalResolver
from severian.auth.store import ApiKeyStore
from severian.domain.models import Scope


def _scope(**overrides: str) -> Scope:
    base = {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }
    base.update(overrides)
    return Scope(**base)


def _make_request(headers: dict[str, str] | None = None) -> Request:
    """Build a minimal Starlette Request with headers."""
    raw_headers: list[tuple[bytes, bytes]] = []
    if headers:
        for k, v in headers.items():
            raw_headers.append((k.lower().encode(), v.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw_headers,
        "client": ("127.0.0.1", 50000),
    }
    return Request(scope)


# -- disabled mode ---------------------------------------------------------

def test_disabled_resolver_returns_none(tmp_path: Path):
    resolver = PrincipalResolver(store=None)
    assert resolver.enabled is False
    req = _make_request(headers={})
    assert resolver(req) is None


def test_disabled_resolver_ignores_authorization_header(tmp_path: Path):
    resolver = PrincipalResolver(store=None)
    req = _make_request(headers={"authorization": "Bearer anything"})
    assert resolver(req) is None


# -- enabled mode ----------------------------------------------------------

def test_enabled_resolver_resolves_valid_bearer(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("valid-key", scope=_scope())
    resolver = PrincipalResolver(store=store)
    req = _make_request(headers={"authorization": "Bearer valid-key"})
    principal = resolver(req)
    assert principal is not None
    assert principal.tenant_id == "t1"
    store.close()


def test_enabled_resolver_rejects_missing_header(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("valid-key", scope=_scope())
    resolver = PrincipalResolver(store=store)
    req = _make_request(headers={})
    with pytest.raises(HTTPException) as exc:
        resolver(req)
    assert exc.value.status_code == 401
    store.close()


def test_enabled_resolver_rejects_wrong_scheme(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("valid-key", scope=_scope())
    resolver = PrincipalResolver(store=store)
    req = _make_request(headers={"authorization": "Basic dXNlcjpwYXNz"})
    with pytest.raises(HTTPException) as exc:
        resolver(req)
    assert exc.value.status_code == 401
    store.close()


def test_enabled_resolver_rejects_empty_bearer(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("valid-key", scope=_scope())
    resolver = PrincipalResolver(store=store)
    req = _make_request(headers={"authorization": "Bearer "})
    with pytest.raises(HTTPException) as exc:
        resolver(req)
    assert exc.value.status_code == 401
    store.close()


def test_enabled_resolver_rejects_unknown_key(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("valid-key", scope=_scope())
    resolver = PrincipalResolver(store=store)
    req = _make_request(headers={"authorization": "Bearer not-a-real-key"})
    with pytest.raises(HTTPException) as exc:
        resolver(req)
    assert exc.value.status_code == 401
    store.close()


def test_enabled_resolver_rejects_revoked_key(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("revoked-key", scope=_scope(), key_id="k1")
    store.revoke("revoked-key")
    resolver = PrincipalResolver(store=store)
    req = _make_request(headers={"authorization": "Bearer revoked-key"})
    with pytest.raises(HTTPException) as exc:
        resolver(req)
    assert exc.value.status_code == 401
    store.close()


def test_resolver_scheme_is_case_insensitive(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("valid-key", scope=_scope())
    resolver = PrincipalResolver(store=store)
    req = _make_request(headers={"authorization": "bearer valid-key"})
    principal = resolver(req)
    assert principal is not None
    store.close()


def test_resolver_returns_correct_principal_for_different_keys(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("key-a", scope=_scope(tenant_id="a"))
    store.add_key("key-b", scope=_scope(tenant_id="b"))
    resolver = PrincipalResolver(store=store)
    a = resolver(_make_request(headers={"authorization": "Bearer key-a"}))
    b = resolver(_make_request(headers={"authorization": "Bearer key-b"}))
    assert a is not None and b is not None
    assert a.tenant_id == "a"
    assert b.tenant_id == "b"
    store.close()
