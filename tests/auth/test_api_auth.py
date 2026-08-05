"""Tests for SEC-01 HTTP API authentication.

RED-first: the API must reject unauthenticated requests with 401 when
auth is enabled, resolve the principal from the Bearer token, and 403
on cross-tenant claims. The existing ``principal`` static binding path
must remain intact.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from severian.api.app import create_app
from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.auth.principal_resolver import PrincipalResolver
from severian.auth.store import ApiKeyStore
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


@dataclass
class FakeGateway:
    model_id: str = "local/test-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _scope_payload(tenant: str = "t1", profile: str = "p1") -> dict[str, str]:
    return {
        "tenant_id": tenant,
        "profile_id": profile,
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }


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


def _scope(tenant: str = "t1", profile: str = "p1") -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id=profile,
        collection_id="c1",
        agent_id="a1",
        session_id="s1",
    )


# -- auth disabled: existing behaviour preserved ---------------------------

def test_auth_disabled_allows_unauthenticated(tmp_path: Path):
    """RED→GREEN: without a principal resolver, requests work as before."""
    service, repo, fts, vector = _build_service(tmp_path)
    app = create_app(
        service=service,
        gateway=lambda: FakeGateway(),
        principal_resolver=None,
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/observations",
            json={"scope": _scope_payload(), "content": "no auth needed", "source": {}},
        )
        assert response.status_code == 201
    fts.close()
    vector.close()
    repo.close()


# -- auth enabled: fail-closed --------------------------------------------

@pytest.fixture
def authed_client(tmp_path: Path):
    """Client with auth enabled and a key bound to t1/p1."""
    service, repo, fts, vector = _build_service(tmp_path)
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("test-key-t1", scope=_scope())
    store.add_key("test-key-t2", scope=_scope(tenant="t2", profile="p2x"))
    resolver = PrincipalResolver(store=store)
    app = create_app(
        service=service,
        gateway=lambda: FakeGateway(),
        principal_resolver=resolver,
    )
    with TestClient(app) as client:
        yield client, store
    fts.close()
    vector.close()
    repo.close()
    store.close()


def test_auth_enabled_rejects_unauthenticated_request(authed_client):
    """RED: unauthenticated request must be 401 (not 201)."""
    client, _ = authed_client
    response = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "should be rejected", "source": {}},
    )
    assert response.status_code == 401


def test_auth_enabled_rejects_wrong_scheme(authed_client):
    client, _ = authed_client
    response = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "x", "source": {}},
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert response.status_code == 401


def test_auth_enabled_rejects_invalid_key(authed_client):
    client, _ = authed_client
    response = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "x", "source": {}},
        headers={"Authorization": "Bearer not-a-real-key"},
    )
    assert response.status_code == 401


def test_auth_enabled_accepts_valid_key(authed_client):
    client, _ = authed_client
    response = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "authorized ingest", "source": {}},
        headers={"Authorization": "Bearer test-key-t1"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["scope"]["tenant_id"] == "t1"
    assert body["scope"]["profile_id"] == "p1"


def test_auth_enabled_rejects_cross_tenant_claim(authed_client):
    """The key is bound to t1/p1; claiming t2 must be 403."""
    client, _ = authed_client
    response = client.post(
        "/api/v1/observations",
        json={
            "scope": _scope_payload(tenant="t2"),
            "content": "should be forbidden",
            "source": {},
        },
        headers={"Authorization": "Bearer test-key-t1"},
    )
    assert response.status_code == 403


def test_auth_enabled_rejects_cross_profile_claim(authed_client):
    client, _ = authed_client
    response = client.post(
        "/api/v1/observations",
        json={
            "scope": _scope_payload(profile="other"),
            "content": "should be forbidden",
            "source": {},
        },
        headers={"Authorization": "Bearer test-key-t1"},
    )
    assert response.status_code == 403


def test_auth_enabled_search_rejects_no_auth(authed_client):
    client, _ = authed_client
    response = client.post(
        "/api/v1/search",
        json={"scope": _scope_payload(), "query": "x", "limit": 5, "derive": False},
    )
    assert response.status_code == 401


def test_auth_enabled_search_with_valid_key(authed_client):
    client, _ = authed_client
    # Ingest first.
    client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "Ada founded a company", "source": {}},
        headers={"Authorization": "Bearer test-key-t1"},
    )
    response = client.post(
        "/api/v1/search",
        json={"scope": _scope_payload(), "query": "Ada", "limit": 5, "derive": True},
        headers={"Authorization": "Bearer test-key-t1"},
    )
    assert response.status_code == 200
    assert response.json()["hits"]


def test_auth_enabled_healthz_does_not_require_auth(authed_client):
    """Health endpoints are unauthenticated (liveness/readiness)."""
    client, _ = authed_client
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


def test_auth_enabled_revoked_key_rejected(authed_client):
    client, store = authed_client
    store.add_key("to-revoke", scope=_scope(), key_id="rev-me")
    # Confirm it works.
    r = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "x", "source": {}},
        headers={"Authorization": "Bearer to-revoke"},
    )
    assert r.status_code == 201
    # Revoke and retry.
    assert store.revoke("to-revoke") is True
    r2 = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "y", "source": {}},
        headers={"Authorization": "Bearer to-revoke"},
    )
    assert r2.status_code == 401


def test_auth_enabled_different_keys_different_principals(authed_client):
    """key-t1 (t1/p1) and key-t2 (t2/p2x) resolve to different tenants."""
    client, _ = authed_client
    # t1 key can ingest into t1.
    r1 = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "tenant one", "source": {}},
        headers={"Authorization": "Bearer test-key-t1"},
    )
    assert r1.status_code == 201
    # t2 key must claim t2.
    r2 = client.post(
        "/api/v1/observations",
        json={
            "scope": _scope_payload(tenant="t2", profile="p2x"),
            "content": "tenant two",
            "source": {},
        },
        headers={"Authorization": "Bearer test-key-t2"},
    )
    assert r2.status_code == 201
    # t2 key claiming t1 must 403.
    r3 = client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "cross", "source": {}},
        headers={"Authorization": "Bearer test-key-t2"},
    )
    assert r3.status_code == 403


def test_auth_enabled_get_observation_rejects_no_auth(authed_client):
    client, _ = authed_client
    response = client.get(
        "/api/v1/observations/some-id",
        params=_scope_payload(),
    )
    assert response.status_code == 401
