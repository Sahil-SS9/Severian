"""R2/R3 integration: native backend through the real search() path.

Proves the native integration returns the same VectorHit IDs, order and
canonical scores as the pure-Python path through the public
LocalVectorIndex.search() contract — with SQLite row fetch included —
and that scope isolation remains Python-owned.

Requires the severian-native wheel (skipped when absent).
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

pytest.importorskip("severian_native")

from severian.domain.hits import VectorHit  # noqa: E402
from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402

SCOPE_A = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")
SCOPE_B = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="other")


def _memory(memory_id: str, content: str, scope: Scope) -> Memory:
    return Memory(
        id=memory_id,
        scope=scope,
        kind="derived",
        content=content,
        source_observation_ids=["obs-1"],
        model_id="hash-16",
        prompt_version="v1",
        confidence=0.9,
    )


@pytest.fixture()
def index(tmp_path: Path) -> Iterator[LocalVectorIndex]:
    idx = LocalVectorIndex(tmp_path / "v.db", HashEmbedding(dimension=16))
    idx.upsert_many([
        _memory("m1", "the red fox runs fast through the forest", SCOPE_A),
        _memory("m2", "the blue whale swims slowly in the ocean", SCOPE_A),
        _memory("m3", "red fox hunting prey at dusk", SCOPE_A),
        _memory("m4", "quantum entanglement measurement protocol", SCOPE_A),
        _memory("m5", "recipe for sourdough bread with rye flour", SCOPE_A),
        _memory("m6", "other session content entirely", SCOPE_B),
    ])
    yield idx
    idx.close()


def _python_hits(index: LocalVectorIndex, scope: Scope, query: str, limit: int) -> list[VectorHit]:
    """Force the pure-Python path via SEVERIAN_VECTOR_BACKEND=python."""
    index._native_accel = None
    old = os.environ.get("SEVERIAN_VECTOR_BACKEND")
    os.environ["SEVERIAN_VECTOR_BACKEND"] = "python"
    try:
        return list(index.search(scope, query, limit=limit))
    finally:
        if old is None:
            os.environ.pop("SEVERIAN_VECTOR_BACKEND", None)
        else:
            os.environ["SEVERIAN_VECTOR_BACKEND"] = old


def _rust_hits(index: LocalVectorIndex, scope: Scope, query: str, limit: int) -> list[VectorHit]:
    index._native_accel = None
    old = os.environ.get("SEVERIAN_VECTOR_BACKEND")
    os.environ["SEVERIAN_VECTOR_BACKEND"] = "rust"
    try:
        return list(index.search(scope, query, limit=limit))
    finally:
        if old is None:
            os.environ.pop("SEVERIAN_VECTOR_BACKEND", None)
        else:
            os.environ["SEVERIAN_VECTOR_BACKEND"] = old


@pytest.mark.parametrize("limit", [1, 2, 3, 5, 10])
def test_native_and_python_return_identical_hits(index: LocalVectorIndex, limit: int) -> None:
    for query in ("red fox", "ocean whale", "bread recipe", "quantum", "nothing matches here"):
        py = _python_hits(index, SCOPE_A, query, limit)
        rs = _rust_hits(index, SCOPE_A, query, limit)
        assert [(h.memory_id, h.score) for h in py] == [(h.memory_id, h.score) for h in rs], (
            f"backend mismatch for {query!r} limit={limit}"
        )
    # Rust backend is reported through diagnostics after use.
    diag = index.backend_diagnostics()
    assert diag["selected"] == "rust"
    assert diag["native_version"] is not None


def test_scope_isolation_preserved_with_native(index: LocalVectorIndex) -> None:
    """m6 lives in SCOPE_B — native search in SCOPE_A must never see it."""
    hits = _rust_hits(index, SCOPE_A, "other session content entirely", 10)
    assert all(h.memory_id != "m6" for h in hits)
    hits_b = _rust_hits(index, SCOPE_B, "other session content entirely", 10)
    assert hits_b and hits_b[0].memory_id == "m6"


def test_auto_selects_rust_and_records(index: LocalVectorIndex) -> None:
    """auto -> rust when the wheel is installed, visible in diagnostics."""
    index._native_accel = None
    old = os.environ.get("SEVERIAN_VECTOR_BACKEND")
    if old is not None:
        os.environ.pop("SEVERIAN_VECTOR_BACKEND")
    try:
        hits = index.search(SCOPE_A, "red fox", limit=2)
        assert hits  # non-empty, deterministic
        diag = index.backend_diagnostics()
        assert diag["selected"] == "rust"
        assert isinstance(diag["calls"], int) and diag["calls"] >= 1
    finally:
        if old is not None:
            os.environ["SEVERIAN_VECTOR_BACKEND"] = old


def test_native_failure_fails_closed_under_rust(index: LocalVectorIndex) -> None:
    """Explicit rust mode never silently falls back on native failure."""
    import severian_native  # type: ignore[import-untyped]

    original = severian_native.exact_top_k

    def broken(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated native crash")

    severian_native.exact_top_k = broken
    try:
        index._native_accel = None
        old = os.environ.get("SEVERIAN_VECTOR_BACKEND")
        os.environ["SEVERIAN_VECTOR_BACKEND"] = "rust"
        try:
            with pytest.raises(RuntimeError):
                index.search(SCOPE_A, "red fox", limit=2)
        finally:
            if old is None:
                os.environ.pop("SEVERIAN_VECTOR_BACKEND", None)
            else:
                os.environ["SEVERIAN_VECTOR_BACKEND"] = old
    finally:
        severian_native.exact_top_k = original
