"""ChromaDB adapter for the provider benchmark harness.

Self-hosted / open-source external provider lane. ChromaDB is imported
lazily so the Severian core package never depends on it; the adapter is
only usable when ``chromadb`` is importable in the runtime environment
(see ``scripts/run_benchmark.py`` which activates the comparison venv).

Security note: ChromaDB 1.5.9 has an unpatched critical pre-auth RCE
(CVE-2026-45829, CVSS 10.0). This adapter uses the PersistentClient in
process with a local directory only — never exposed on a network. Do
not run this adapter against a remotely reachable Chroma instance.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from severian.benchmarks.runner import ProviderQueryResponse


class ChromaAdapter:
    """Benchmark adapter backed by a local ChromaDB PersistentClient."""

    def __init__(
        self,
        *,
        provider_id: str,
        workdir: str | None = None,
        collection_prefix: str = "bench",
    ) -> None:
        import chromadb  # type: ignore[import-not-found]

        self.provider_id = provider_id
        self._client = chromadb.PersistentClient(path=str(Path(workdir or "/tmp/bench-chroma")))
        # One collection per scope: Chroma has no native scope filter, so
        # we encode the five-dimensional boundary in the collection name.
        self._collections: dict[str, Any] = {}
        self._prefix = collection_prefix

    @staticmethod
    def _collection_name(scope: str) -> str:
        # scope "t1/p1/c1/a1/s1" -> "bench_t1_p1_c1_a1_s1"
        return "bench_" + scope.replace("/", "_")

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        # Chroma has no native supersession. The stale-fact gate is
        # exactly what this lane is expected to fail honestly.
        name = self._collection_name(scope)
        if name not in self._collections:
            try:
                self._collections[name] = self._client.get_or_create_collection(name)
            except Exception:
                self._collections[name] = self._client.create_collection(name)
        self._collections[name].upsert(ids=[document_id], documents=[content])

    def query(self, query: str, *, limit: int, scope: str) -> ProviderQueryResponse:
        name = self._collection_name(scope)
        if name not in self._collections:
            # No collection for this scope yet — return empty (no leakage).
            return ProviderQueryResponse(items=[], cost_usd=0.0)
        collection = self._collections[name]
        result = collection.query(query_texts=[query], n_results=limit)
        ids: list[str] = (result.get("ids") or [[]])[0] or []
        distances: list[float] = (result.get("distances") or [[]])[0] or []
        items: list[dict[str, Any]] = []
        for index, document_id in enumerate(ids):
            items.append(
                {
                    "document_id": document_id,
                    "citation": document_id,
                    "score": -distances[index] if index < len(distances) else 0.0,
                }
            )
        return ProviderQueryResponse(items=items, cost_usd=0.0)

    def close(self) -> None:
        pass

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "backend": "chroma-persistent-client",
            "model": "chroma-default-embedding",
            "prompt_version": "n/a",
            "security_advisory": "CVE-2026-45829 (critical, unpatched) — local-only use",
        }
