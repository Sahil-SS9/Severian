"""ChromaDB adapter for the provider benchmark harness.

Self-hosted / open-source external provider lane. ChromaDB is imported
lazily so the Severian core package never depends on it; the adapter is
only usable when ``chromadb`` is importable in the runtime environment
(see ``scripts/run_benchmark.py`` which activates the comparison venv).

Security note: ChromaDB 1.5.9 has an unpatched critical pre-auth RCE
(CVE-2026-45829, CVSS 10.0). This adapter uses the PersistentClient in
process with a local directory only — never exposed on a network. Do
not run this adapter against a remotely reachable Chroma instance.

Safety contract (auditor C-01 hardening):
1. ``run_namespace`` is validated as a single safe token (``[A-Za-z0-9._-]+``).
   Path separators, ``..``, and absolute paths are REJECTED — a namespace
   can never escape the supplied workdir root.
2. A namespace may only be claimed ONCE per workdir root (per process).
   Re-claiming the same namespace in the same root raises ``ValueError``:
   the store is never silently reused, so a prior invocation's documents
   can never bleed into a new run (contamination independent of order).
3. ``reset()`` only deletes a directory this adapter instance created or
   explicitly owns — proven by an ownership marker file written at
   construction. Basename prefix checks are defense-in-depth only; they
   are NEVER sufficient on their own (a user directory could share the
   prefix).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from severian.benchmarks.runner import ProviderQueryResponse

_SAFE_NAMESPACE = re.compile(r"^[A-Za-z0-9._-]+$")
_OWNERSHIP_MARKER = ".severian-bench-owned"
_CLAIM_MARKER = ".severian-bench-claimed"


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
        self._created_root = workdir is None
        if workdir is None:
            import tempfile

            workdir = tempfile.mkdtemp(prefix="severian-bench-chroma-")
        root = Path(workdir).resolve()
        # Safety 1: namespace must be a single safe token. A namespace like
        # "../escaped" or "/abs/path" would otherwise escape the supplied
        # root and let a run write/read outside its benchmark-owned area.
        # "." and ".." are also rejected explicitly (they are path
        # components, not namespaces — even though they match the char
        # class, using them would resolve to the root itself / its parent).
        if run_namespace is not None and (
            not _SAFE_NAMESPACE.fullmatch(run_namespace) or run_namespace in {".", ".."}
        ):
            raise ValueError(
                f"invalid run_namespace {run_namespace!r}: must match "
                rf"{_SAFE_NAMESPACE.pattern} (single token, no path separators, "
                "no '.' or '..')"
            )
        # Safety 2: a namespace may be claimed only once per workdir root.
        # Same-namespace reuse would silently share the store between
        # invocations — the exact contamination path the reviewer
        # reproduced (prior run's documents visible in a new run).
        # The claim is a FILE marker in the namespace dir, so it holds
        # across processes (a fresh benchmark process cannot silently
        # reuse a namespace claimed by an earlier one) and is released
        # by close(). Note: uv run may reuse a long-lived interpreter, so
        # an in-memory registry would falsely persist — the file marker
        # is authoritative.
        self._namespace_claim: Path | None = None
        if run_namespace is not None:
            claim_path = Path(workdir) / run_namespace / _CLAIM_MARKER
            if claim_path.exists():
                raise ValueError(
                    f"run_namespace {run_namespace!r} already claimed in workdir {workdir}; "
                    "each benchmark invocation must own a unique namespace "
                    "(same-namespace reuse would contaminate the store)"
                )
            self._namespace_claim = claim_path
        # When BOTH workdir and run_namespace are provided, the namespace
        # is applied at the PERSISTENCE level too (workdir/<namespace>),
        # not just in collection names.
        if run_namespace:
            root = root / run_namespace
            root.mkdir(parents=True, exist_ok=True)
            if self._namespace_claim is not None:
                self._namespace_claim.write_text(f"claimed by {provider_id}\n")
        self._workdir = str(root)
        # Safety 3: ownership marker. reset() may ONLY delete a directory
        # that is genuinely benchmark-owned:
        #   - the adapter created the root itself (workdir=None → mkdtemp),
        #     or
        #   - the root carries our marker written at creation, or
        #   - the namespace subdir was created by this adapter (run_namespace
        #     under an explicit root → created fresh).
        # A pre-existing directory WITHOUT our marker is NOT owned, even if
        # its basename matches the benchmark prefix (a user directory could
        # share it) — reset() refuses it.
        marker = root / _OWNERSHIP_MARKER
        if self._created_root:
            self._owns_marker = True
            if not marker.exists():
                marker.write_text("benchmark-owned; safe to clear\n")
        elif marker.exists():
            try:
                self._owns_marker = marker.read_text().strip() == "benchmark-owned; safe to clear"
            except OSError:
                self._owns_marker = False
        else:
            existed_before = root.exists()
            root.mkdir(parents=True, exist_ok=True)
            if not existed_before:
                marker.write_text("benchmark-owned; safe to clear\n")
                self._owns_marker = True
            else:
                self._owns_marker = False
        self._client = chromadb.PersistentClient(path=str(root))
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
        # NOTE: the namespace claim is deliberately NOT released here.
        # A namespace in a workdir root is single-use PERMANENTLY: releasing
        # it on close() would let a later invocation reuse the same store
        # directory, where the prior run's documents still persist — the
        # exact contamination the reviewer reproduced. Benchmark scripts
        # always use fresh roots (mkdtemp default) or unique run-ids, so
        # permanent single-use never blocks a legitimate run.
        pass

    def reset(self) -> None:
        """Clear the benchmark-owned Chroma store.

        Safety: only a directory carrying this adapter's ownership marker
        (written at construction) is touched. The basename prefix check is
        defense-in-depth, never sufficient on its own: a user directory
        that merely shares the prefix is REFUSED. Arbitrary user paths are
        never deleted.
        """
        root = Path(self._workdir)
        if not root.exists():
            return
        if not self._owns_marker:
            raise PermissionError(
                f"refusing to clear {root}: directory was not created by a "
                "benchmark adapter (no ownership marker from creation)"
            )
        marker = root.name
        if not (marker.startswith("severian-bench-chroma-") or marker.startswith("bench-")):
            raise PermissionError(
                f"refusing to clear non-benchmark Chroma root: {root} "
                "(adapter may only reset benchmark-owned directories)"
            )
        import shutil
        import tempfile

        shutil.rmtree(root)
        new_root = Path(tempfile.mkdtemp(prefix="severian-bench-chroma-"))
        (new_root / _OWNERSHIP_MARKER).write_text("benchmark-owned; safe to clear\n")
        self._workdir = str(new_root)
        self._owns_marker = True
        self._client = __import__("chromadb").PersistentClient(path=str(new_root))
        self._collections = {}

    def describe(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "backend": "chroma-persistent-client",
            "model": "chroma-default-embedding",
            "prompt_version": "n/a",
            "security_advisory": "CVE-2026-45829 (critical, unpatched) — local-only use",
        }
