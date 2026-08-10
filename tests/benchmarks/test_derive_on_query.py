"""Retrieval bake-off latency integrity (zero-debt P0.3).

The embedding bake-off measures RETRIEVAL. A severian lane whose query
defaults derive=True would derive up to 50 observations INTO the index
per scored query, growing it mid-run — measured contamination
(granite-97m p95 79s vs hash 1.6s; latencies grew 36ms → ~53s across
150 queries).

Contract under test:
- derive_on_query=False forces derive=False on adapters that accept it;
- adapters without a derive param (baseline, chroma) are untouched;
- the embedding lane warms the index to full derivation BEFORE scoring,
  so hot queries see a stable, fully-derived index.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


from severian.benchmarks.manifest import BenchmarkManifest  # noqa: E402
from severian.benchmarks.runner import (  # noqa: E402
    BenchmarkRunner,
    ProviderQueryResponse,
)


def _manifest() -> BenchmarkManifest:
    """Real schema-v2 manifest with BOTH populations (P0-D validator)."""
    payload: dict[str, Any] = {
        "schema_version": 2,
        "manifest_id": "derive-contract",
        "name": "derive contract",
        "corpus": {
            "id": "c1", "version": "1", "seed": 1,
            "documents": [
                {"id": "d1", "content": "fact one: the service handles 7 requests",
                 "scope": "t1/p1/c1/a1/s1"},
                {"id": "d2", "content": "fact two: the service handles 14 requests",
                 "scope": "t1/p1/c1/a1/s1"},
            ],
        },
        "queries": [
            {"id": "q1", "kind": "fact", "query": "service handles 7 requests",
             "scope": "t1/p1/c1/a1/s1", "answerable": True,
             "expected_evidence": [{"document_id": "d1", "weight": 1.0}]},
            {"id": "q2", "kind": "temporal", "query": "service handles 14 requests",
             "scope": "t1/p1/c1/a1/s1", "answerable": True,
             "expected_evidence": [{"document_id": "d2", "weight": 1.0}]},
            {"id": "q-neg", "kind": "negative", "query": "unrelated topic",
             "scope": "t1/p1/c1/a1/s1", "answerable": False,
             "expected_evidence": []},
            {"id": "q-adv", "kind": "adversarial", "query": "adversarial probe",
             "scope": "t1/p1/c1/a1/s1", "answerable": False,
             "expected_evidence": []},
        ],
        "providers": [
            {"id": "p1", "kind": "severian", "deployment": "local",
             "backend": "sqlite", "model": {"id": "hash", "prompt_version": "v1"},
             "config": {}},
        ],
        "metrics": ["recall_at_10", "mrr", "ndcg_at_10", "citation_precision",
                    "stale_fact_rate", "temporal_accuracy", "cross_scope_leakage",
                    "unsupported_answer_rate", "negative_false_positive_rate",
                    "latency_p95_ms", "cost_per_query_usd"],
        "release_gate": {"require_external_providers": 0},
    }
    return BenchmarkManifest.model_validate(payload)


class _DeriveAdapter:
    """Severian-like: query accepts derive=."""

    def __init__(self) -> None:
        self.calls: list[bool | None] = []

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        pass

    def ingest_batch(self, documents: Iterable[tuple[str, str]], *, scope: str) -> None:
        pass

    def query(self, query: str, *, limit: int, scope: str, query_kind: str | None = None,
              derive: bool | None = None) -> ProviderQueryResponse:
        self.calls.append(derive)
        return ProviderQueryResponse(
            items=[{"document_id": "d1", "citation": "d1", "score": 1.0}],
            cost_usd=0.0,
        )

    def close(self) -> None:
        pass


class _PlainAdapter:
    """Baseline-like: query has no derive param."""

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        pass

    def ingest_batch(self, documents: Iterable[tuple[str, str]], *, scope: str) -> None:
        pass

    def query(self, query: str, *, limit: int, scope: str, query_kind: str | None = None) -> ProviderQueryResponse:
        return ProviderQueryResponse(
            items=[{"document_id": "d1", "citation": "d1", "score": 1.0}],
            cost_usd=0.0,
        )

    def close(self) -> None:
        pass


def test_derive_on_query_false_forces_derive_false() -> None:
    adapter = _DeriveAdapter()
    runner = BenchmarkRunner(manifest=_manifest(), adapters={"p1": adapter},
                             allow_missing=True, derive_on_query=False)
    runner._run_provider("p1")  # noqa: SLF001
    assert adapter.calls == [False, False, False, False], (
        f"severian-like adapter must receive derive=False on every scored "
        f"query, got {adapter.calls}"
    )


def test_plain_adapter_never_receives_derive_kwarg() -> None:
    adapter = _PlainAdapter()
    runner = BenchmarkRunner(manifest=_manifest(), adapters={"p1": adapter},
                             allow_missing=True, derive_on_query=False)
    # Should not raise TypeError: no derive kwarg is injected.
    runner._run_provider("p1")  # noqa: SLF001


def test_default_derive_on_query_preserves_legacy_behaviour() -> None:
    adapter = _DeriveAdapter()
    runner = BenchmarkRunner(manifest=_manifest(), adapters={"p1": adapter},
                             allow_missing=True)
    runner._run_provider("p1")  # noqa: SLF001
    assert adapter.calls == [None, None, None, None], (
        "legacy default must not force derive=False (only opt-out forces it)"
    )
