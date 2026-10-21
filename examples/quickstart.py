"""Severian in 30 lines: save memories for two users, search one, and build a cited context.

    uv run python examples/quickstart.py

Runs fully offline (deterministic hash embedding, a temporary SQLite store).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from severian.application.worker import DerivationWorker
from severian.composition import build_bundle
from severian.domain.models import Scope

# Every read and write names its full scope: tenant / profile / collection / agent / session.
alice = Scope(tenant_id="acme", profile_id="alice", collection_id="main", agent_id="assistant", session_id="s1")
bob = Scope(tenant_id="acme", profile_id="bob", collection_id="main", agent_id="assistant", session_id="s1")

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    bundle = build_bundle(database=root / "severian.db", fts=root / "severian.fts", vectors=root / "severian.vec")
    service = bundle.service

    # 1. Save what the user said. Raw observations are kept for good; memories are derived from them.
    service.ingest_observation(alice, content="I live in Manchester and I work at Acme.", source={"uri": "chat://1"})
    service.ingest_observation(bob, content="I live in Bristol.", source={"uri": "chat://2"})
    DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)

    # 2. Search Alice's memory. Every hit cites the observation it came from.
    for hit in service.search(alice, query="Where do I live?", limit=3):
        print(f"alice: {hit.content}  (source {hit.source_observation_ids[0][:8]})")

    # 3. Scope is the security boundary: Bob never sees Alice's memories.
    leaked = [hit for hit in service.search(bob, query="Manchester Acme", limit=10) if "Acme" in hit.content]
    print("bob can see alice's memories:", bool(leaked))

    # 4. Hand the answering model a bounded, cited context block.
    hits = service.search(alice, query="Where does the user work?", limit=3)
    print(service.assemble_context(alice, hits, query="Where does the user work?"))

    bundle.close()
