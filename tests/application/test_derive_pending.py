"""derive_pending contract (zero-debt P0.3 / warmup path).

- derives fresh observations into memories WITHOUT running retrieval;
- respects the per-call bound (OPS-03 semantics: fresh work only);
- projects the outbox delta after deriving (same as search(derive=True));
- idempotent: a second call derives zero and does not re-project.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.application.service import BuildMemoryService  # noqa: E402
from severian.domain.models import (  # noqa: E402
    Collection,
    Observation,
    Scope,
)
from severian.infrastructure.sqlite import SQLiteRepository  # noqa: E402


class _FakeGateway:
    """Derives exactly one memory per observation (no LLM needed)."""

    model_id = "fake/derive"
    prompt_version = "test-v1"

    def derive(self, content: str, *, policy: dict | None = None) -> list[dict]:
        return [
            {
                "kind": "fact",
                "content": content,
                "confidence": 0.9,
            }
        ]


@pytest.fixture()
def env(tmp_path: Path):
    from severian.application.derivation import DerivationService
    from severian.application.ingestion import IngestionService
    from severian.application.projections import ProjectionCoordinator
    from severian.application.retrieval import RetrievalService
    from severian.infrastructure.fts import FTS5Index
    from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

    repo = SQLiteRepository(tmp_path / "canonical.db")
    repo.initialize()
    repo.create_collection(Collection(id="c", tenant_id="t", name="Primary"))
    scope = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
    for i in range(120):
        repo.add_observation(
            Observation(id=f"obs-{i}", scope=scope, content=f"fact {i}: queue depth {i*7}")
        )
    lexical = FTS5Index(str(tmp_path / "fts.db"))
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    projection = ProjectionCoordinator(repo, lexical=lexical, vector=vector)
    retrieval = RetrievalService(repo, lexical, vector)
    derivation = DerivationService(repository=repo)
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=retrieval,
        projection=projection,
        derivation=derivation,
    )
    yield repo, service, scope, projection
    repo.close()


def test_derive_pending_respects_bound_and_projects(env) -> None:
    repo, service, scope, projection = env
    derived = service.derive_pending(scope, _FakeGateway(), max_observations=50)
    assert derived == 50
    assert repo.count_memories(scope) == 50
    # Projection converged: outbox for this scope is drained.
    pending = [e for e in repo.pending_outbox() if e.scope == scope]
    assert pending == []


def test_derive_pending_is_idempotent(env) -> None:
    repo, service, scope, projection = env
    service.derive_pending(scope, _FakeGateway(), max_observations=50)
    second = service.derive_pending(scope, _FakeGateway(), max_observations=50)
    assert second == 50  # the NEXT 50 fresh observations
    assert repo.count_memories(scope) == 100
    third = service.derive_pending(scope, _FakeGateway(), max_observations=50)
    assert third == 20  # only 20 remain
    assert repo.count_memories(scope) == 120
    assert service.derive_pending(scope, _FakeGateway(), max_observations=50) == 0


def test_derive_pending_no_gateway_raises(env) -> None:
    repo, service, scope, projection = env
    with pytest.raises(ValueError, match="gateway"):
        service.derive_pending(scope, None)  # type: ignore[arg-type]
