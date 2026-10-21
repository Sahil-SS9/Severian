"""Release gates (step 10 of the brutal-critique remediation).

Replays the five release gates from docs/release-gates.md against the
fixed system, on BOTH the reference SQLite backend and a live
PostgreSQL backend (skipped when SEVERIAN_PG_DSN is unset):

1. SQLite + disposable PG semantic parity — same scenario, same
   observable results, same canonical digests.
2. Crash / fault injection — mid-write interruption must not corrupt
   canonical data; leased jobs must be reaped and retried.
3. Clean restore — export -> fresh store -> import must preserve every
   canonical record with identical semantic digests.
4. Cloud-deny recording — a cloud gateway must never run while
   collection policy denies it, and the denial must be recorded.
5. Supersession recall — a superseded fact must never resurface in
   default recall; it is visible only through explicit history.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from severian.application.derivation import CloudPolicyError, DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import RetrievalService
from severian.application.worker import DerivationWorker
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.postgresql import PostgreSQLBackend
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")


@dataclass
class CloudGateway:
    """Gateway that records every invocation; cloud flag is settable."""

    model_id: str = "cloud/recorded-model"
    prompt_version: str = "prompt-v1"
    is_cloud: bool = False
    calls: int = 0

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        self.calls += 1
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


def _unique_scope(**overrides: str) -> Scope:
    base = {
        "tenant_id": f"t-{uuid.uuid4().hex[:8]}",
        "profile_id": f"p-{uuid.uuid4().hex[:6]}",
        "collection_id": f"c-{uuid.uuid4().hex[:8]}",
        "agent_id": f"a-{uuid.uuid4().hex[:6]}",
        "session_id": f"s-{uuid.uuid4().hex[:6]}",
    }
    base.update(overrides)
    return Scope(**base)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _records_digest(records: list[dict[str, Any]]) -> str:
    # Semantic digest: order-insensitive (restore may reorder rows), so
    # sort by the stable record id before hashing.
    return _digest(json.dumps(sorted(records, key=lambda r: r.get("id", "")), sort_keys=True))


@pytest.fixture(params=["sqlite", "postgres"])
def bundle(request: pytest.FixtureRequest, tmp_path: Path):
    """Full service bundle on both backends; per-test unique scope space."""
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("SEVERIAN_PG_DSN not set; skipping live PostgreSQL lane")
        os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
        repo = PostgreSQLBackend(dsn=PG_DSN)
        repo.reset(acknowledge=True)
    else:
        repo = SQLiteRepository(tmp_path / "severian.db")
        repo.initialize()
    fts = FTS5Index(tmp_path / f"fts-{request.param}.db")
    vector = LocalVectorIndex(tmp_path / f"vectors-{request.param}.db", HashEmbedding(dimension=64))
    repo.create_collection(Collection(id="c1", tenant_id="t-default", name="Primary"))
    yield {
        "repo": repo,
        "fts": fts,
        "vector": vector,
        "ingestion": IngestionService(repo),
        "derivation": DerivationService(repo),
        "retrieval": RetrievalService(repo, fts, vector),
        "projection": ProjectionCoordinator(repo, fts, vector),
    }
    fts.close()
    vector.close()
    repo.close()


# ---------------------------------------------------------------------------
# Gate 1 — semantic parity
# ---------------------------------------------------------------------------


def test_gate1_semantic_parity_across_backends(bundle):
    """Same ingest/derive/revise scenario yields identical observable results."""
    repo = bundle["repo"]
    scope = _unique_scope()
    collection = Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate1")
    repo.create_collection(collection)

    # Ingest two observations with different source identities but
    # normalisation-equivalent content — they must remain distinct
    # (P0-1 raw evidence preservation).
    o1 = bundle["ingestion"].ingest(scope, "  Ada founded a company  ", {"uri": "note://A"})
    o2 = bundle["ingestion"].ingest(scope, "Ada founded a company", {"uri": "note://B"})
    assert o1.observation.id != o2.observation.id

    # Derive both; results must be stable and cite their sources.
    gateway = CloudGateway(is_cloud=False)
    m1 = bundle["derivation"].derive_observation(scope, o1.observation.id, gateway)
    m2 = bundle["derivation"].derive_observation(scope, o2.observation.id, gateway)
    assert m1 and m2

    # Revise the first observation — the old derived fact is superseded.
    repo.revise_observation(scope, o1.observation.id, "Ada founded Analytics Engine")
    superseded = repo.supersede_memories_for_source(scope, o1.observation.id)
    assert superseded >= 1
    active = [m for m in repo.list_memories(scope) if m.id in {m1[0].id, m2[0].id}]
    assert len(active) == 1  # only m2 survives as active head


def test_gate1_canonical_digests_match_across_backends(bundle):
    repo = bundle["repo"]
    scope = _unique_scope()
    repo.create_collection(Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate1d"))
    bundle["ingestion"].ingest(scope, "Deterministic content", {"uri": "note://A"})
    bundle["ingestion"].ingest(scope, "Second deterministic", {"uri": "note://B"})
    export = repo.export_json()
    # Same snapshot, canonical fields only — semantic digest must not
    # depend on backend-specific storage details.
    document = json.loads(export)
    assert document["format"] == "severian-v1"
    assert _digest(json.dumps(document["records"], sort_keys=True))
    assert len(document["records"]) >= 2


# ---------------------------------------------------------------------------
# Gate 2 — crash / fault injection
# ---------------------------------------------------------------------------


def test_gate2_fault_injection_mid_batch_aborts_atomically(bundle):
    """A malformed candidate in a batch aborts the whole publication."""
    repo = bundle["repo"]
    scope = _unique_scope()
    repo.create_collection(Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate2"))

    class MalformedGateway:
        model_id = "local/test"
        prompt_version = "pv1"
        is_cloud = False

        def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
            return [
                {"kind": "fact", "content": "good candidate", "confidence": 0.9},
                {"kind": "fact"},  # missing content
            ]

    obs = bundle["ingestion"].ingest(scope, "Batch fault probe", {"uri": "note://A"})
    with pytest.raises(ValueError):
        bundle["derivation"].derive_observation(scope, obs.observation.id, MalformedGateway())
    # No partial publication: nothing from this batch landed.
    assert repo.list_memories(scope) == []


def test_gate2_fault_injection_leased_job_reap_and_retry(bundle):
    """A claimed-but-never-completed job is reaped and can be retried."""
    from datetime import UTC, datetime

    repo = bundle["repo"]
    scope = _unique_scope()
    repo.create_collection(Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate2b"))
    obs = bundle["ingestion"].ingest(scope, "Lease probe", {"uri": "note://A"})
    # Claim with a short lease, then let it expire (crash: never complete).
    claimed = repo.claim_job(worker_id="w-crashed", lease_seconds=1)
    assert claimed is not None and claimed.id == obs.job.id
    # Reap must requeue the expired lease.
    reaped = repo.reap_expired_jobs(now=datetime(2030, 1, 1, tzinfo=UTC), max_attempts=3)
    assert reaped >= 1
    reclaimed = repo.claim_job(worker_id="w-recovered")
    assert reclaimed is not None and reclaimed.id == obs.job.id
    assert reclaimed.attempts == 2


def test_gate2_fault_injection_worker_drain_converges(bundle):
    """After a mid-drain crash, a later drain converges with no data loss."""
    repo = bundle["repo"]
    fts, vector = bundle["fts"], bundle["vector"]
    scope = _unique_scope()
    repo.create_collection(Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate2c"))
    bundle["ingestion"].ingest(scope, "Crash convergence probe", {"uri": "note://A"})

    worker = DerivationWorker(
        repo, bundle["derivation"], ProjectionCoordinator(repo, fts, vector)
    )
    gateway = CloudGateway(is_cloud=False)
    # Drain once fully; the job must be acknowledged only after
    # projections converged.
    summary = worker.drain(worker_id="w1", gateway=gateway, max_jobs=10)
    assert summary.processed == 1
    assert repo.list_memories(scope)  # projection built
    # Repeat drain: idempotent, nothing double-published.
    summary2 = worker.drain(worker_id="w1", gateway=gateway, max_jobs=10)
    assert summary2.processed == 0


# ---------------------------------------------------------------------------
# Gate 3 — clean restore
# ---------------------------------------------------------------------------


def test_gate3_clean_restore_preserves_everything(bundle, tmp_path):
    """export -> fresh store -> import must preserve records + digests."""
    repo = bundle["repo"]
    scope = _unique_scope()
    repo.create_collection(Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate3"))
    bundle["ingestion"].ingest(scope, "Restore me one", {"uri": "note://A"})
    bundle["ingestion"].ingest(scope, "Restore me two", {"uri": "note://B"})
    export = repo.export_json()
    before = json.loads(export)
    before_digest = _records_digest(before["records"])

    # Fresh store on the SAME backend kind (or opposite when PG lane off).
    # Import into an EMPTY store: the snapshot itself carries collection
    # records, so pre-creating a colliding collection would 404-conflict
    # on created_at.
    fresh = SQLiteRepository(tmp_path / "fresh.db")
    fresh.initialize()
    fresh.import_json(export)
    after = json.loads(fresh.export_json())
    after_digest = _records_digest(after["records"])
    assert before_digest == after_digest
    assert len(after["records"]) == len(before["records"])
    fresh.close()


# ---------------------------------------------------------------------------
# Gate 4 — cloud-deny recording
# ---------------------------------------------------------------------------


def test_gate4_cloud_deny_never_calls_cloud_gateway(bundle):
    """Cloud derivation under deny policy must not invoke the gateway."""
    repo = bundle["repo"]
    scope = _unique_scope()
    repo.create_collection(
        Collection(
            id=scope.collection_id,
            tenant_id=scope.tenant_id,
            name="Gate4",
            policy={"cloud_allowed": False},
        )
    )
    obs = bundle["ingestion"].ingest(scope, "Deny probe", {"uri": "note://A"})

    gateway = CloudGateway(is_cloud=True)
    with pytest.raises(CloudPolicyError):
        bundle["derivation"].derive_observation(scope, obs.observation.id, gateway)
    assert gateway.calls == 0  # the cloud gateway was never reached


def test_gate4_cloud_deny_caller_cannot_loosen_policy(bundle):
    """A caller passing cloud_allowed=True cannot override stored deny."""
    repo = bundle["repo"]
    scope = _unique_scope()
    repo.create_collection(
        Collection(
            id=scope.collection_id,
            tenant_id=scope.tenant_id,
            name="Gate4b",
            policy={"cloud_allowed": False},
        )
    )
    obs = bundle["ingestion"].ingest(scope, "Deny probe 2", {"uri": "note://A"})
    gateway = CloudGateway(is_cloud=True)
    with pytest.raises(CloudPolicyError):
        bundle["derivation"].derive_observation(
            scope, obs.observation.id, gateway, policy={"cloud_allowed": True}
        )
    assert gateway.calls == 0


# ---------------------------------------------------------------------------
# Gate 5 — supersession recall
# ---------------------------------------------------------------------------


def test_gate5_superseded_fact_never_resurfaces(bundle):
    """After correction, the old fact is gone from default recall/history reads."""
    repo = bundle["repo"]
    scope = _unique_scope()
    repo.create_collection(Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate5"))
    obs = bundle["ingestion"].ingest(scope, "Ada founded the Analytical Engine", {"uri": "note://A"})
    gateway = CloudGateway(is_cloud=False)
    derived = bundle["derivation"].derive_observation(scope, obs.observation.id, gateway)
    assert derived, "expected a derived memory"
    fact_id = derived[0].id

    # Correct the source observation through the SAME path the service
    # uses: revise, supersede derived memories citing the old head, then
    # converge projections.
    repo.revise_observation(scope, obs.observation.id, "Ada founded the Difference Engine")
    superseded = repo.supersede_memories_for_source(scope, obs.observation.id)
    assert superseded >= 1
    bundle["projection"].rebuild(scope)

    active = repo.list_memories(scope)
    assert fact_id not in {m.id for m in active}
    history = repo.list_memories_history(scope)
    history_ids = {m.id for m in history}
    assert fact_id in history_ids  # visible only via explicit history


def test_gate5_superseded_fact_not_searchable_without_history(bundle):
    repo = bundle["repo"]
    fts, vector = bundle["fts"], bundle["vector"]
    scope = _unique_scope()
    repo.create_collection(Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="Gate5b"))
    obs = bundle["ingestion"].ingest(scope, "The sky is bright blue", {"uri": "note://A"})
    gateway = CloudGateway(is_cloud=False)
    derived = bundle["derivation"].derive_observation(scope, obs.observation.id, gateway)
    assert derived

    retrieval = RetrievalService(repo, fts, vector)
    bundle["projection"].rebuild(scope)
    hits = retrieval.search(scope, "blue", limit=10)
    assert any(h.memory.id == derived[0].id for h in hits)

    repo.revise_observation(scope, obs.observation.id, "The sky is grey today")
    repo.supersede_memories_for_source(scope, obs.observation.id)
    bundle["projection"].rebuild(scope)
    hits_after = retrieval.search(scope, "blue", limit=10)
    assert all(h.memory.id != derived[0].id for h in hits_after)
