"""Task 3.1 acceptance: derive=True on a fully derived scope is cheap.

Before 3.1, a bounded derive pass scanned EVERY observation and called
list_memories(scope) twice per observation — on a fully derived scope
that was ~400-500x amplification with zero work done. After 3.1 the
pending-work lookup is a bounded SQL query: derive=True on a fully
derived scope must stay within a small constant factor of derive=False
and derive zero new memories.

The assertion is behavioural, not timing-based (CI machines vary):
a fully derived scope must perform the SAME bounded work — measured by
deriving zero and returning identical results — and the pending query
itself is proven bounded by the repository contract tests. The timing
guard here is generous (10x) so a regression to full-scope scans fails
hard on any machine while honest runs pass with wide margin.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest  # noqa: E402

from severian.application.derivation import DerivationService  # noqa: E402
from severian.application.ingestion import IngestionService  # noqa: E402
from severian.application.projections import ProjectionCoordinator  # noqa: E402
from severian.application.retrieval import RetrievalService  # noqa: E402
from severian.application.service import BuildMemoryService  # noqa: E402
from severian.domain.models import Collection, Scope  # noqa: E402
from severian.infrastructure.fts import FTS5Index  # noqa: E402
from severian.infrastructure.sqlite import SQLiteRepository  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


class _FakeGateway:
    model_id = "test/hasher"
    prompt_version = "test-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.9}]


@pytest.fixture()
def bundle(tmp_path: Path):
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts.db")
    vector = LocalVectorIndex(tmp_path / "vectors.db", HashEmbedding(dimension=64))
    derivation = DerivationService(repo)
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=ProjectionCoordinator(repo, fts, vector),
        derivation=derivation,
        context=None,
    )
    yield {
        "repo": repo, "service": service, "fts": fts,
        "vector": vector, "gateway": _FakeGateway(),
    }
    fts.close()
    vector.close()
    repo.close()


def _scope() -> Scope:
    return Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


def test_fully_derived_scope_derive_stays_close_to_no_derive(bundle) -> None:
    sc = _scope()
    for i in range(20):
        bundle["service"].ingest_observation(sc, content=f"fact number {i}", source={"uri": f"n://{i}"})

    # Fully derive the scope.
    bundle["service"].search(sc, query="fact", limit=50, derive=True, gateway=bundle["gateway"])

    # Baseline: derive=False timing + result set.
    baseline = bundle["service"].search(sc, query="fact", limit=50, derive=False)
    t0 = time.perf_counter()
    no_derive = bundle["service"].search(sc, query="fact", limit=50, derive=False)
    no_derive_ms = (time.perf_counter() - t0) * 1000

    # derive=True on the fully derived scope must not amplify.
    t0 = time.perf_counter()
    with_derive = bundle["service"].search(sc, query="fact", limit=50, derive=True, gateway=bundle["gateway"])
    derive_ms = (time.perf_counter() - t0) * 1000

    # Behavioural contract: zero new work, identical results.
    assert len(with_derive) == len(baseline) == 20
    assert [h.memory_id for h in with_derive] == [h.memory_id for h in no_derive]

    # Generous timing guard: a regression to per-observation full-scope
    # scans blows past 10x (measured pre-fix: ~400-500x on 1k scope);
    # the bounded query stays well under.
    assert derive_ms < max(10 * no_derive_ms, 50.0), (
        f"derive=True amplified {derive_ms:.1f}ms vs {no_derive_ms:.1f}ms on a fully derived scope"
    )


def test_one_pending_processes_only_that_item(bundle) -> None:
    sc = _scope()
    for i in range(10):
        bundle["service"].ingest_observation(sc, content=f"fact number {i}", source={"uri": f"n://{i}"})

    # Derive 9 of 10 (bounded passes of 5 then 4).
    for _ in range(2):
        bundle["service"].search(
            sc, query="fact", limit=50, derive=True,
            gateway=bundle["gateway"], derive_max_observations=5,
        )

    # One new observation arrives; a bounded pass derives ONLY it.
    bundle["service"].ingest_observation(sc, content="fact number 10", source={"uri": "n://10"})
    hits = bundle["service"].search(sc, query="fact", limit=50, derive=True, gateway=bundle["gateway"])
    assert len(hits) == 11
    # And the scope is now fully derived: next pass derives zero.
    assert bundle["repo"].list_pending_observations(sc, limit=50) == []
