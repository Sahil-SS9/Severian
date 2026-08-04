"""Severian adapter for the provider benchmark harness.

Wraps the canonical composition root so the adapter exercises the same
wiring a production user gets (repository + ingestion + derivation +
projection + retrieval). Document IDs are carried in the observation's
``source`` dict so returned memories can be mapped back to corpus
documents deterministically.
"""
from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from severian.benchmarks.adapters.scope import parse_scope
from severian.benchmarks.runner import ProviderQueryResponse

# Optional; imported lazily so the module can be imported without a backend.
_SEVERIAN_COMPOSE_CACHE: dict[str, Any] = {}


class SeverianAdapter:
    """Benchmark adapter backed by the Severian service bundle."""

    model_id = "severian/hash+fts"
    prompt_version = "prompt-v1"
    is_cloud = False

    def __init__(
        self,
        *,
        provider_id: str,
        backend: str = "sqlite",
        dsn: str | None = None,
        workdir: str | None = None,
        dimension: int = 256,
        embedding: Any | None = None,
        min_vector_score: float | None = 0.15,
        reset_db: bool = False,
    ) -> None:
        from severian.composition import build_bundle

        self.provider_id = provider_id
        self.backend = backend
        self.dsn = dsn
        self._min_vector_score = min_vector_score
        self._embedding = embedding
        self._root = Path(workdir or tempfile.mkdtemp(prefix="severian-bench-"))
        self._root.mkdir(parents=True, exist_ok=True)
        database = self._root / "severian.db"
        fts = self._root / "severian.fts"
        vectors = self._root / "severian.vec"
        if backend == "postgresql":
            if not dsn:
                raise ValueError("postgresql backend requires a DSN")
            self._bundle = build_bundle(
                backend="postgresql",
                dsn=dsn,
                database=database,
                fts=fts,
                vectors=vectors,
                embedding=self._embedding,
            )
        else:
            self._bundle = build_bundle(
                backend="sqlite",
                database=database,
                fts=fts,
                vectors=vectors,
                embedding=self._embedding,
            )
        # A local, deterministic derivation gateway (never cloud).
        self._gateway = _LocalHashGateway()
        # Benchmark runs must start from a clean database: a PG lane that
        # inherits leftover rows from an earlier run re-derives them on
        # every query and the numbers stop measuring this corpus. reset()
        # requires the test-owned-db guard; the benchmark scripts set
        # SEVERIAN_PG_ALLOW_RESET for their dedicated benchmark DSNs.
        if reset_db:
            if backend != "postgresql":
                raise ValueError("reset_db only applies to the postgresql backend")
            self._bundle.repository.reset(acknowledge=True)
        # observation_id -> document_id map for trace resolution.
        self._obs_to_doc: dict[str, str] = {}
        # scope -> collection created
        self._collections: set[str] = set()

    # -- provider protocol -------------------------------------------------

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        # see _supersede below for the correction path
        self._ingest_observation(document_id, content, scope, supersedes)

    def ingest_batch(
        self,
        documents: Iterable[tuple[str, str]],
        *,
        scope: str,
    ) -> None:
        # No vectorized batch path on the canonical store yet — the
        # per-doc protocol path IS the measured production path (roadmap
        # A-4 compares this against Chroma's batch override).
        for document_id, content in documents:
            self.ingest(document_id, content, scope=scope)

    def _ingest_observation(
        self, document_id: str, content: str, scope: str, supersedes: Iterable[str]
    ) -> None:
        from severian.domain.models import Collection, Scope

        parts = parse_scope(scope)
        scoped = Scope(
            tenant_id=parts[0],
            profile_id=parts[1],
            collection_id=parts[2],
            agent_id=parts[3],
            session_id=parts[4],
        )
        collection_key = f"{scoped.tenant_id}/{scoped.collection_id}"
        if collection_key not in self._collections:
            try:
                self._bundle.repository.create_collection(
                    Collection(id=scoped.collection_id, tenant_id=scoped.tenant_id, name="bench")
                )
            except Exception:
                pass  # already exists
            self._collections.add(collection_key)
        view = self._bundle.service.ingest_observation(
            scoped,
            content=content,
            source={"bench_document_id": document_id, "uri": f"bench://{document_id}"},
        )
        self._obs_to_doc[view.observation_id] = document_id
        # Exercise the platform's correction machinery: every doc that a
        # new observation supersedes is routed through the canonical
        # revise path so its observation is marked superseded (derive
        # skips it) and its memories are tombstoned.
        for superseded_document_id in supersedes:
            self._supersede(scoped, superseded_document_id, content)

    def _supersede(self, scope: Any, superseded_document_id: str, replacement_content: str) -> None:
        """Mark the superseded corpus document's observation as revised.

        ``revise_observation`` sets ``superseded_by_id`` on the old head,
        which the derive pass skips, and tombstones any memories already
        derived from it (P0-5 correction propagation). Naive providers
        have no equivalent — the stale-fact gate is the differentiator.
        """
        from severian.domain.errors import ConflictError

        for obs_id, doc_id in list(self._obs_to_doc.items()):
            if doc_id != superseded_document_id:
                continue
            try:
                self._bundle.service.revise_observation(scope, obs_id, replacement_content)
            except ConflictError:
                # Already superseded by an earlier ingest; idempotent.
                continue

    def query(
        self,
        query: str,
        *,
        limit: int,
        scope: str,
        query_kind: str | None = None,
        derive: bool = True,
    ) -> ProviderQueryResponse:
        from severian.domain.models import Scope

        parts = parse_scope(scope)
        scoped = Scope(
            tenant_id=parts[0],
            profile_id=parts[1],
            collection_id=parts[2],
            agent_id=parts[3],
            session_id=parts[4],
        )
        hits = self._bundle.service.search(
            scoped,
            query=query,
            limit=limit,
            derive=derive,
            gateway=self._gateway,
            min_vector_score=self._min_vector_score,
        )
        items: list[dict[str, Any]] = []
        for hit in hits:
            # Resolve each hit to corpus document ids via its source
            # observations. Every corpus doc was ingested through this
            # adapter, so the map is complete for real hits.
            doc_ids = [
                self._obs_to_doc[obs_id]
                for obs_id in hit.source_observation_ids
                if obs_id in self._obs_to_doc
            ]
            for document_id in doc_ids:
                items.append(
                    {
                        "document_id": document_id,
                        "citation": document_id,
                        "score": hit.fused_score,
                        "memory_id": hit.memory_id,
                    }
                )
        # Temporal probes ("what WAS the old X?") ask for historical
        # facts. Active-head search suppresses superseded memories by
        # design (P0-5) — the old fact's derived memory is tombstoned
        # when a revision lands. But the canonical store RETAINS the
        # original observation as immutable raw evidence (P0-1), so the
        # historical fact is still queryable at the observation layer.
        # Fall back to a lexical match over ALL observations in scope
        # (including superseded ones) so temporal probes are answered
        # from the evidence the platform actually keeps.
        if query_kind == "temporal":
            observations = self._bundle.repository.list_observations(scoped)
            history_items: list[dict[str, Any]] = []
            query_tokens = {tok.casefold() for tok in re.findall(r"[\w]+", query)}
            for observation in observations:
                doc_id = self._obs_to_doc.get(observation.id)
                if doc_id is None:
                    continue
                obs_tokens = {tok.casefold() for tok in re.findall(r"[\w]+", observation.content)}
                overlap = query_tokens & obs_tokens
                # Require at least two overlapping content tokens (stop
                # words like "was"/"the" alone are not a match).
                if len(overlap) >= 2:
                    history_items.append(
                        {
                            "document_id": doc_id,
                            "citation": doc_id,
                            "score": float(len(overlap)),
                            "observation_id": observation.id,
                        }
                    )
            items = history_items + items
        # De-duplicate by document_id preserving order.
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for item in items:
            if item["document_id"] not in seen:
                seen.add(item["document_id"])
                deduped.append(item)
        # Cost: fully local, no per-query API charge.
        return ProviderQueryResponse(items=deduped[:limit], cost_usd=0.0)

    def close(self) -> None:
        self._bundle.close()

    # -- metadata ----------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "backend": self.backend,
            "model": self.model_id,
            "prompt_version": self.prompt_version,
            "dsn_set": bool(self.dsn),
        }


class _LocalHashGateway:
    """Deterministic local derivation (never cloud, policy-respecting)."""

    model_id = "severian/hash+fts"
    prompt_version = "prompt-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]
