"""Large-scope vector search: NumPy top-k must equal the exact reference order, ties included."""
from __future__ import annotations

from pathlib import Path

import pytest

import severian.infrastructure.vector as V
from severian.domain.models import Memory, Scope
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

pytest.importorskip("numpy")


def _scope() -> Scope:
    return Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")


def _mem(i: int, content: str) -> Memory:
    return Memory(id=f"mem-{i:05d}", kind="fact", content=content, scope=_scope(),
                  source_observation_ids=[f"obs-{i}"], model_id="test/hasher", prompt_version="test-v1", confidence=0.9)


def _index(tmp_path: Path, n: int) -> LocalVectorIndex:
    idx = LocalVectorIndex(tmp_path / "v.db", HashEmbedding(dimension=64))
    words = ["backup", "region", "deploy", "gateway", "memory", "rerank", "cron", "bot"]
    # many exact duplicates -> many tied scores, so tie-break order is exercised
    idx.upsert_many([_mem(i, f"{words[i % 8]} {words[(i * 3) % 8]} job {i % 37}") for i in range(n)])
    return idx


@pytest.mark.parametrize("limit", [1, 5, 20, 200])
def test_numpy_topk_matches_reference_on_large_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int) -> None:
    idx = _index(tmp_path, 3000)
    out = {}
    for mode in (False, True):
        monkeypatch.setattr(V, "_NUMPY_AVAILABLE", mode)
        out[mode] = [(h.memory_id, h.score) for h in idx.search(_scope(), "backup region job 4", limit=limit)]
    assert out[True] == out[False]
    assert len(out[True]) == limit


def test_numpy_topk_limit_larger_than_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    idx = _index(tmp_path, 7)
    monkeypatch.setattr(V, "_NUMPY_AVAILABLE", True)
    assert len(idx.search(_scope(), "backup", limit=50)) == 7
