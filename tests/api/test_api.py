"""Tests for the versioned FastAPI surface.

Scope isolation, malformed request handling, and routing through the
canonical application service are tested. Adapters MUST NOT bypass
``MemoryService`` so the API layer is exercised against the same
service used by SDK/CLI.
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


@pytest.fixture
def client(tmp_path: Path):
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
    app = create_app(service=service, gateway=lambda: FakeGateway())
    with TestClient(app) as test_client:
        yield test_client, service, FakeGateway()
    fts.close()
    vector.close()
    repo.close()


def test_healthz_reports_ok(client):
    test_client, _service, _gateway = client
    response = test_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_readyz_reports_components(client):
    test_client, _service, _gateway = client
    response = test_client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["components"]["repository"] == "ok"
    assert body["components"]["lexical"] == "ok"
    assert body["components"]["vector"] == "ok"


def test_ingest_observation_round_trip(client):
    test_client, _service, _gateway = client
    response = test_client.post(
        "/api/v1/observations",
        json={
            "scope": _scope_payload(),
            "content": "  Hello\r\nworld  ",
            "source": {"uri": "note://1"},
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["content"] == "Hello\nworld"
    assert body["content_hash"]
    assert body["job_kind"] == "derive"

    fetched = test_client.get(f"/api/v1/observations/{body['observation_id']}", params=_scope_payload())
    assert fetched.status_code == 200
    assert fetched.json()["content"] == "Hello\nworld"


def test_ingest_rejects_malformed_scope(client):
    test_client, _service, _gateway = client
    response = test_client.post(
        "/api/v1/observations",
        json={"scope": {"tenant_id": ""}, "content": "x", "source": {}},
    )
    assert response.status_code == 422


def test_ingest_rejects_empty_content(client):
    test_client, _service, _gateway = client
    response = test_client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "  \n\t  ", "source": {}},
    )
    assert response.status_code == 422


def test_get_observation_is_scope_isolated(client):
    test_client, _service, _gateway = client
    created = test_client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "secret", "source": {}},
    ).json()

    cross_profile = test_client.get(
        f"/api/v1/observations/{created['observation_id']}", params=_scope_payload(profile="p2")
    )
    assert cross_profile.status_code == 404

    cross_tenant = test_client.get(
        f"/api/v1/observations/{created['observation_id']}", params=_scope_payload(tenant="t2")
    )
    assert cross_tenant.status_code == 404


def test_search_uses_application_service_and_cites_sources(client):
    test_client, _service, _gateway = client
    created = test_client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "Ada founded a company", "source": {}},
    ).json()
    response = test_client.post(
        "/api/v1/search",
        json={
            "scope": _scope_payload(),
            "query": "Ada founded",
            "limit": 5,
            "derive": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["hits"]
    assert body["hits"][0]["source_observation_ids"] == [created["observation_id"]]
    assert body["hits"][0]["scope"]["profile_id"] == "p1"


def test_search_derives_only_when_requested(client):
    test_client, _service, _gateway = client
    test_client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "Ada founded a company", "source": {}},
    )
    no_derive = test_client.post(
        "/api/v1/search",
        json={"scope": _scope_payload(), "query": "Ada", "limit": 5, "derive": False},
    )
    assert no_derive.status_code == 200
    assert no_derive.json()["hits"] == []


def test_search_rejects_missing_query(client):
    test_client, _service, _gateway = client
    response = test_client.post(
        "/api/v1/search",
        json={"scope": _scope_payload(), "limit": 5},
    )
    assert response.status_code == 422


# -- P0-6: scope is authorization, not a client-supplied selector ---------


def _principal_client(tmp_path: Path):
    """Build a client with a bound principal (tenant t1 / profile p1)."""
    repo = SQLiteRepository(tmp_path / "severian-p.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts-p.db")
    vector = LocalVectorIndex(tmp_path / "vectors-p.db", HashEmbedding(dimension=64))
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=ProjectionCoordinator(repo, fts, vector),
        derivation=DerivationService(repo),
        context=ContextAssembler(),
    )
    app = create_app(
        service=service,
        gateway=lambda: FakeGateway(),
        principal=Scope(
            tenant_id="t1",
            profile_id="p1",
            collection_id="c1",
            agent_id="a1",
            session_id="s1",
        ),
    )
    with TestClient(app) as test_client:
        yield test_client
    fts.close()
    vector.close()
    repo.close()


def test_principal_rejects_cross_tenant_scope(tmp_path):
    # P0-6 repro: a client could claim any tenant/profile; the API bound
    # no principal, so scope was a pure selector. With a principal bound,
    # claiming a foreign tenant must be 403 (authorization), never 422
    # (validation) — otherwise existence can be probed via error codes.
    test_client = next(_principal_client(tmp_path))
    for path, payload in (
        ("/api/v1/observations", {"scope": _scope_payload(tenant="evil"), "content": "x", "source": {}}),
        (
            "/api/v1/search",
            {"scope": _scope_payload(tenant="evil"), "query": "x", "limit": 5, "derive": False},
        ),
    ):
        response = test_client.post(path, json=payload)
        assert response.status_code == 403


def test_principal_rejects_cross_profile_scope(tmp_path):
    test_client = next(_principal_client(tmp_path))
    response = test_client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(profile="other"), "content": "x", "source": {}},
    )
    assert response.status_code == 403


def test_principal_accepts_matching_scope(tmp_path):
    test_client = next(_principal_client(tmp_path))
    response = test_client.post(
        "/api/v1/observations",
        json={"scope": _scope_payload(), "content": "authorized", "source": {}},
    )
    assert response.status_code == 201
    assert response.json()["scope"]["tenant_id"] == "t1"
    assert response.json()["scope"]["profile_id"] == "p1"
