"""Profile the derive path components at 10k scale.

A3 + repro evidence: derive_pending sustains only ~2-3 memories/s
(500-memory pass every ~170s). Ingest is 181 docs/s. Find WHICH
component dominates: list_pending_observations, per-observation
derive_observation (extraction, publish_derivation), pending_outbox
materialisation, projection.apply, ack_outbox_many, count_memories.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.run_scale_benchmark import generate_corpus  # noqa: E402
from severian.benchmarks.adapters.severian import SeverianAdapter  # noqa: E402

QUERY_SCOPE = "t/bench/c/bench-agent/bench-session"


def main() -> None:
    bucket = 3_000
    docs = generate_corpus(bucket)
    workdir = tempfile.mkdtemp(prefix="severian-prof-")
    adapter = SeverianAdapter(provider_id="severian-sqlite", backend="sqlite", workdir=workdir)
    for doc_id, content in docs:
        adapter.ingest(doc_id, content, scope=QUERY_SCOPE)

    from severian.domain.models import Scope
    scoped = Scope(tenant_id="t", profile_id="bench", collection_id="c",
                   agent_id="bench-agent", session_id="bench-session")
    service = adapter._bundle.service
    repo = adapter._bundle.repository
    gateway = adapter._gateway

    # 1. list_pending_observations
    t0 = time.perf_counter()
    pending = repo.list_pending_observations(scoped, limit=500)
    print(f"list_pending_observations(500): {(time.perf_counter()-t0)*1000:.1f}ms")

    # 2. per-observation derive_observation (one fresh obs)
    t0 = time.perf_counter()
    for _ in range(20):
        service._derivation.derive_observation(scoped, pending[0].id, gateway, policy={})
    print(f"derive_observation x20: {(time.perf_counter()-t0)*1000:.1f}ms avg "
          f"{(time.perf_counter()-t0)*1000/20:.1f}ms/obs")

    # 3. pending_outbox materialisation
    t0 = time.perf_counter()
    evs = repo.pending_outbox()
    print(f"pending_outbox: {len(evs)} events in {(time.perf_counter()-t0)*1000:.1f}ms")

    # 4. full derive_pending pass
    t0 = time.perf_counter()
    derived = service.derive_pending(scoped, gateway=gateway, max_observations=500)
    pass_s = time.perf_counter() - t0
    print(f"derive_pending pass: {derived} derived in {pass_s:.1f}s "
          f"({derived/pass_s:.1f}/s effective)")

    # 5. count_memories
    t0 = time.perf_counter()
    repo.count_memories(scoped)
    print(f"count_memories: {(time.perf_counter()-t0)*1000:.1f}ms")

    adapter.close()


if __name__ == "__main__":
    main()
