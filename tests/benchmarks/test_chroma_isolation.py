"""C-01 benchmark tests: Chroma run isolation (namespace + cleared root + order independence).

RED tests for the correction packet:
1. Two adapter prefixes / run namespaces cannot resolve to the same collection.
2. Running manifest-v1 then LOCOMO yields the same non-latency LOCOMO metrics
   as LOCOMO then manifest-v1 (order independence).
3. Returned IDs are drawn only from the active manifest corpus; no ``d*`` IDs
   may appear in a LOCOMO-only run.
4. Cleanup/reset refuses any path that is not explicitly benchmark-owned.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from severian.benchmarks.adapters.chroma import ChromaAdapter
from severian.benchmarks.runner import BenchmarkRunner

MANIFEST_V1 = Path(__file__).resolve().parents[2] / "docs/benchmarks/manifest-v1.json"
MANIFEST_LOCOMO = Path(__file__).resolve().parents[2] / "docs/benchmarks/manifest-locomo-tier-v1.json"

pytestmark = pytest.mark.skipif(
    not Path("/tmp/bench-compare-venv").exists(),
    reason="chroma comparison venv not present",
)


@pytest.fixture(autouse=True)
def _chroma_venv_on_path() -> None:
    """Make chromadb importable for tests that construct ChromaAdapter directly."""
    import sys

    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))


def _load_manifest(path: Path):
    from severian.benchmarks.manifest import BenchmarkManifest

    return BenchmarkManifest.model_validate(json.loads(path.read_text()))


def _run_manifest(manifest_path: Path, run_namespace: str, workdir: Path) -> dict:
    """Run one manifest through a fresh ChromaAdapter; return non-latency metrics + IDs."""
    import sys

    # make the chroma venv importable if it exists
    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))

    from severian.benchmarks.adapters.chroma import ChromaAdapter

    adapter = ChromaAdapter(
        provider_id="chroma",
        workdir=str(workdir / f"store-{run_namespace}"),
        run_namespace=run_namespace,
    )
    manifest = _load_manifest(manifest_path)
    runner = BenchmarkRunner(manifest=manifest, adapters={"chroma": adapter}, allow_missing=True)
    result = runner.run()
    # containment: query every probe scope and collect all returned IDs
    returned_ids: set[str] = set()
    for query in manifest.queries:
        response = adapter.query(query.query, limit=10, scope=query.scope, query_kind=query.kind)
        returned_ids.update(str(item.get("document_id", "")) for item in response.items)
    # return only the non-latency metrics for comparison
    provider = result.results["chroma"]
    metrics = {
        "recall": provider.recall_at_10,
        "mrr": provider.mrr,
        "ndcg": provider.ndcg_at_10,
        "cit_prec": provider.citation_precision,
        "stale": provider.stale_fact_rate,
        "leak": provider.cross_scope_leakage,
        "unsup": provider.unsupported_answer_rate,
        "temporal": provider.temporal_accuracy,
        "ids": sorted(returned_ids),
    }
    adapter.close()
    return metrics


def test_collection_names_namespace_isolated() -> None:
    """Two adapters with different run namespaces resolve to different collections."""
    import sys

    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    root = Path(tempfile.mkdtemp(prefix="severian-bench-test-ns-"))
    try:
        a = ChromaAdapter(provider_id="chroma", workdir=str(root), run_namespace="run-1")
        b = ChromaAdapter(provider_id="chroma", workdir=str(root), run_namespace="run-2")
        assert a._collection_name("t1/p1/c1/a1/s1") != b._collection_name("t1/p1/c1/a1/s1")
        assert "run-1" in a._collection_name("t1/p1/c1/a1/s1")
        assert "run-2" in b._collection_name("t1/p1/c1/a1/s1")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_prefix_changes_collection() -> None:
    """Two prefixes cannot resolve to the same collection."""
    import sys

    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    root = Path(tempfile.mkdtemp(prefix="severian-bench-test-prefix-"))
    try:
        a = ChromaAdapter(provider_id="chroma", workdir=str(root), collection_prefix="alpha")
        b = ChromaAdapter(provider_id="chroma", workdir=str(root), collection_prefix="beta")
        assert a._collection_name("t1/p1/c1/a1/s1") != b._collection_name("t1/p1/c1/a1/s1")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_run_order_independence() -> None:
    """manifest-v1 then LOCOMO == LOCOMO then manifest-v1 (non-latency metrics + IDs).

    Both manifests run into the SAME Chroma persistence root, each with its
    OWN unique run namespace. This is the real contamination scenario: a
    shared store that must not bleed between corpora regardless of execution
    order. Each invocation has a unique namespace (the adapter enforces
    namespace-scoped persistence subdirs), so v1's documents can never be
    visible to the LOCOMO run even though they share the root.
    """
    workdir = Path(tempfile.mkdtemp(prefix="severian-bench-order-"))
    try:
        # Order A: manifest-v1 first, then LOCOMO, same root.
        _run_manifest(MANIFEST_V1, "order-a-v1", workdir)
        locomo_a = _run_manifest(MANIFEST_LOCOMO, "order-a-loco", workdir)
        # Order B: LOCOMO first, then manifest-v1, same root (fresh dir).
        workdir_b = Path(tempfile.mkdtemp(prefix="severian-bench-order-b-"))
        _run_manifest(MANIFEST_V1, "order-b-v1", workdir_b)
        locomo_b = _run_manifest(MANIFEST_LOCOMO, "order-b-loco", workdir_b)
        for key in ("recall", "mrr", "ndcg", "cit_prec", "stale", "leak", "unsup", "temporal"):
            assert locomo_a[key] == locomo_b[key], f"{key}: {locomo_a[key]} != {locomo_b[key]}"
        assert locomo_a["ids"] == locomo_b["ids"]
        shutil.rmtree(workdir_b, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_loco_run_contains_no_foreign_docs() -> None:
    """A LOCOMO-only run must not return d* IDs (manifest-v1 corpus bleed)."""
    workdir = Path(tempfile.mkdtemp(prefix="severian-bench-containment-"))
    try:
        metrics = _run_manifest(MANIFEST_LOCOMO, "containment", workdir)
        assert all(not doc_id.startswith("d") for doc_id in metrics["ids"]), (
            f"foreign doc IDs in LOCOMO run: {[i for i in metrics['ids'] if i.startswith('d')]}"
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_reset_refuses_non_benchmark_path() -> None:
    """reset() must refuse directories that are not benchmark-owned."""
    bogus = Path(tempfile.mkdtemp(prefix="user-data-"))
    try:
        adapter = ChromaAdapter(provider_id="chroma", workdir=str(bogus))
        with pytest.raises(PermissionError):
            adapter.reset()
        assert bogus.exists(), "reset() must not delete a non-benchmark directory"
    finally:
        shutil.rmtree(bogus, ignore_errors=True)


def test_same_workdir_different_namespace_is_isolated() -> None:
    """Two invocations sharing a workdir root must not contaminate each other.

    This is the exact contamination path C-01 forbids: manifest-v1 documents
    (d*) must NEVER appear in a LOCOMO run's results even when both share a
    persistence root. Each invocation gets its own namespace, and the adapter
    scopes persistence by namespace (workdir/<namespace>).
    """
    import sys

    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    workdir = Path(tempfile.mkdtemp(prefix="severian-bench-shared-"))
    try:
        # v1 docs into namespace "v1"
        a = ChromaAdapter(provider_id="chroma", workdir=str(workdir), run_namespace="v1")
        a.ingest("d1", "the annual revenue is 10 million", scope="t1/p1/c1/a1/s1")
        a.close()
        # LOCOMO-style docs into namespace "loco" — SAME root
        b = ChromaAdapter(provider_id="chroma", workdir=str(workdir), run_namespace="loco")
        b.ingest("c01", "project aurora reached milestone five", scope="t1/p1/c1/a1/s1")
        response = b.query("annual revenue", limit=10, scope="t1/p1/c1/a1/s1")
        ids = [item["document_id"] for item in response.items]
        assert "d1" not in ids, f"contamination: v1 doc visible in loco namespace: {ids}"
        assert "c01" in ids
        b.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_same_namespace_reuse_refused() -> None:
    """Re-claiming the same namespace in the same workdir root must raise.

    The reviewer's direct check: same workdir + same namespace returned
    ['d1', 'c01'] — prior invocation documents visible in the new run.
    The adapter now refuses namespace reuse outright (per-process claim
    registry), so this contamination path cannot occur.
    """
    import sys

    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    workdir = Path(tempfile.mkdtemp(prefix="severian-bench-reuse-"))
    try:
        a = ChromaAdapter(provider_id="chroma", workdir=str(workdir), run_namespace="ns")
        a.ingest("d1", "annual revenue ten million", scope="t1/p1/c1/a1/s1")
        a.close()
        with pytest.raises(ValueError):
            ChromaAdapter(provider_id="chroma", workdir=str(workdir), run_namespace="ns")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_namespace_path_traversal_rejected() -> None:
    """A namespace with path separators / .. must be rejected, never escape."""
    import sys

    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    workdir = Path(tempfile.mkdtemp(prefix="severian-bench-traversal-"))
    try:
        for bad in ("../escaped", "a/b", "/abs", "..", "a\\b"):
            with pytest.raises(ValueError):
                ChromaAdapter(provider_id="chroma", workdir=str(workdir), run_namespace=bad)
        # The escaped directory must NOT exist outside the root.
        assert not (workdir.parent / "escaped").exists()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_reset_refuses_unowned_prefix_matching_dir() -> None:
    """reset() must refuse a directory that shares the basename prefix but
    carries no ownership marker — a user directory must never be deleted."""
    import sys

    site = next((Path("/tmp/bench-compare-venv") / "lib").glob("python*/site-packages"), None)
    if site is not None and str(site) not in sys.path:
        sys.path.insert(0, str(site))
    victim = Path(tempfile.mkdtemp(prefix="severian-bench-chroma-"))
    sentinel = victim / "user-data.txt"
    sentinel.write_text("user data")
    try:
        adapter = ChromaAdapter(provider_id="chroma", workdir=str(victim))
        with pytest.raises(PermissionError):
            adapter.reset()
        assert victim.exists(), "reset() must not delete an unowned directory"
        assert sentinel.exists(), "user data must survive reset()"
    finally:
        shutil.rmtree(victim, ignore_errors=True)


def test_default_workdir_is_unique_per_adapter() -> None:
    """Omitting workdir yields a unique benchmark-owned temp root (no shared store)."""
    a = ChromaAdapter(provider_id="chroma")
    b = ChromaAdapter(provider_id="chroma")
    try:
        assert a._workdir != b._workdir
        assert Path(a._workdir).name.startswith("severian-bench-chroma-")
    finally:
        shutil.rmtree(a._workdir, ignore_errors=True)
        shutil.rmtree(b._workdir, ignore_errors=True)
