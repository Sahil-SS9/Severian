"""P1-2 / P1-3 regression tests: readiness truth, digest sync, safe reset,
and the config-driven composition root."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from severian.api.app import create_app
from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

PG_DSN = __import__("os").environ.get("SEVERIAN_PG_DSN", "")


def scope() -> Scope:
    return Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


def _service_bundle(tmp_path: Path) -> ServiceBundle:
    return build_bundle(
        backend="sqlite",
        database=tmp_path / "severian.db",
        fts=tmp_path / "severian.fts",
        vectors=tmp_path / "severian.vec",
    )


# -- P1-2: digest-based projection sync ----------------------------------


def test_projection_status_digest_catches_stale_content(tmp_path):
    # P1-2 repro: in_sync compared counts only — a projection holding a
    # stale row with the same COUNT but different content reported sync.
    bundle = _service_bundle(tmp_path)
    repo = bundle.repository
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    obs = repo.add_observation(Observation(scope=scope(), content="source"))
    memory = repo.add_memory(
        Memory(
            scope=scope(),
            kind="fact",
            content="live fact",
            source_observation_ids=[obs.id],
            model_id="local/model",
            prompt_version="v1",
            confidence=0.8,
        )
    )
    coordinator = ProjectionCoordinator(repo, bundle.lexical, bundle.vector)
    coordinator.rebuild(scope())
    assert coordinator.status(scope()).in_sync

    # Simulate a stale projection row: upsert the SAME memory id into the
    # projections with DIFFERENT content (count unchanged, content
    # divergent). Canonical stays untouched.
    stale = memory.model_copy(update={"content": "STALE content"})
    bundle.lexical.upsert(stale)
    bundle.vector.upsert(stale)

    status = coordinator.status(scope())
    assert status.canonical_memories == status.lexical_entries == status.vector_entries
    assert not status.in_sync  # digest differs even though counts match
    bundle.close()


# -- P1-2: adapter-neutral health + readyz 503 ----------------------------


def test_health_is_adapter_neutral(tmp_path):
    bundle = _service_bundle(tmp_path)
    report = bundle.service.health()
    assert report.ready is True
    assert report.components["repository"] == "ok"
    assert report.components["lexical"] == "ok"
    assert report.components["vector"] == "ok"
    bundle.close()


def test_readyz_returns_503_when_dependency_down(tmp_path):
    # P1-2 repro: readyz always returned HTTP 200 even when ready=False.
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
    app = create_app(service=service, gateway=lambda: _FakeGateway())

    # Break the lexical projection: close its connection so the
    # adapter-neutral health probe reports error. (SQLite reopens the
    # repository on demand, so closing the repo does not fail health;
    # the FTS connection is the reliable fault seam.)
    fts.close()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        liveness = client.get("/healthz")
        assert liveness.status_code == 200  # liveness stays up
    vector.close()


class _FakeGateway:
    model_id = "local/test"
    prompt_version = "p1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content, "confidence": 0.5}]


# -- P1-3: safe reset + composition root ----------------------------------


def test_composition_root_builds_sqlite_bundle(tmp_path):
    bundle = _service_bundle(tmp_path)
    assert bundle.backend == "sqlite"
    assert bundle.service is not None
    assert bundle.gateway is not None
    bundle.close()


@pytest.mark.skipif(not PG_DSN, reason="SEVERIAN_PG_DSN not set")
def test_composition_root_builds_postgresql_bundle(tmp_path):
    import os

    os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
    bundle = build_bundle(backend="postgresql", dsn=PG_DSN)
    assert bundle.backend == "postgresql"
    assert bundle.service is not None
    bundle.close()


def test_postgres_reset_requires_acknowledgement_and_test_ownership(monkeypatch):
    pytest.importorskip("severian.infrastructure.postgresql")
    from severian.infrastructure.postgresql import PostgreSQLBackend

    # Other live tests set the allow-reset override; isolate this test.
    monkeypatch.delenv("SEVERIAN_PG_ALLOW_RESET", raising=False)

    repo = PostgreSQLBackend(dsn="postgresql://user:pass@localhost:5432/production")
    with pytest.raises(ValueError, match="acknowledge"):
        repo.reset()
    with pytest.raises(ValueError, match="test-owned"):
        repo.reset(acknowledge=True)


def test_unknown_backend_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown backend"):
        build_bundle(backend="oracle", database=tmp_path / "x.db")
