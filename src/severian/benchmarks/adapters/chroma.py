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
        run_namespace: str | None = None,
    ) -> None:
        import chromadb  # type: ignore[import-not-found]

        self.provider_id = provider_id
        # C-01 isolation: every benchmark run must own a unique Chroma
        # persistence root (or an explicitly cleared benchmark-owned
        # directory). The default is NO LONGER a shared persistent path —
        # a caller that omits workdir gets a unique benchmark-owned
        # temp root, so corpora can never bleed between runs.
        if workdir is None:
            import tempfile

            workdir = tempfile.mkdtemp(prefix="severian-bench-chroma-")
        self._workdir = workdir
        self._client = chromadb.PersistentClient(path=workdir)
        # One collection per scope: Chroma has no native scope filter, so
        # we encode the five-dimensional boundary in the collection name.
        # C-01: the collection name ALSO carries the run namespace (when
        # provided) so two runs of the same manifest in the same store
        # cannot resolve to the same collection, and a LOCOMO run can
        # never see manifest-v1 documents.
        self._collections: dict[str, Any] = {}
        self._prefix = collection_prefix
        self._run_namespace = run_namespace

    @staticmethod
    def _scope_token(scope: str) -> str:
        # scope "t1/p1/c1/a1/s1" -> "t1_p1_c1_a1_s1"
        return scope.replace("/", "_")

    def _collection_name(self, scope: str) -> str:
        # "bench" + optional run namespace + scope token
        parts = [self._prefix]
        if self._run_namespace:
            parts.append(self._run_namespace)
        parts.append(self._scope_token(scope))
        return "_".join(parts)

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        # Chroma has no native supersession. The stale-fact gate is
        # exactly what this lane is expected to fail honestly.
        self._collection_for(scope).upsert(ids=[document_id], documents=[content])

    def ingest_batch(
        self,
        documents: Iterable[tuple[str, str]],
        *,
        scope: str,
    ) -> None:
        # Vectorized batch path: one upsert call for many documents
        # instead of one call per document. This is the path Chroma's
        # docs recommend for throughput; the scale lane measures the
        # difference against the per-doc path (roadmap A-4).
        collection = self._collection_for(scope)
        pairs = list(documents)
        if not pairs:
            return
        ids, contents = zip(*pairs, strict=True)
        collection.upsert(ids=list(ids), documents=list(contents))

    def _collection_for(self, scope: str) -> Any:
        name = self._collection_name(scope)
        if name not in self._collections:
            try:
                self._collections[name] = self._client.get_or_create_collection(name)
            except Exception:
                self._collections[name] = self._client.create_collection(name)
        return self._collections[name]

    def query(self, query: str, *, limit: int, scope: str, query_kind: str | None = None) -> ProviderQueryResponse:
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

    def reset(self) -> None:
        """Clear the benchmark-owned Chroma store.

        Safety: only the directory this adapter created (or was explicitly
        given at construction) is touched, and only if it looks like a
        benchmark-owned root. Arbitrary user paths are refused — this
        cleanup is bounded to benchmark state, never a user directory.
        """
        root = Path(self._workdir)
        if not root.exists():
            return
        marker = root.name
        if not (marker.startswith("severian-bench-chroma-") or marker.startswith("bench-")):
            raise PermissionError(
                f"refusing to clear non-benchmark Chroma root: {root} "
                "(adapter may only reset benchmark-owned directories)"
            )
        import shutil

        shutil.rmtree(root)
        import tempfile

        self._workdir = tempfile.mkdtemp(prefix="severian-bench-chroma-")
        self._client = __import__("chromadb").PersistentClient(path=self._workdir)
        self._collections = {}

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "backend": "chroma-persistent-client",
            "model": "chroma-default-embedding",
            "prompt_version": "n/a",
            "security_advisory": "CVE-2026-45829 (critical, unpatched) — local-only use",
        }
