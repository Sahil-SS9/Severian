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

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


from severian.benchmarks.runner import (  # noqa: E402
    BenchmarkRunner,
    ProviderQueryResponse,
)


class _DeriveAdapter:
    """Severian-like: query accepts derive=."""

    def __init__(self) -> None:
        self.calls: list[bool | None] = []

    def ingest(self, doc_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        pass

    def ingest_batch(self, documents: Iterable[tuple[str, str]], *, scope: str) -> None:
        pass

    def query(self, query: str, *, limit: int, scope: str, query_kind: str | None = None,
              derive: bool | None = None):
        self.calls.append(derive)
        return ProviderQueryResponse(
            items=[{"document_id": "d1", "citation": "d1", "score": 1.0}],
            cost_usd=0.0,
        )

    def close(self) -> None:
        pass


class _PlainAdapter:
    """Baseline-like: query has no derive param."""

    def ingest(self, doc_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        pass

    def ingest_batch(self, documents: Iterable[tuple[str, str]], *, scope: str) -> None:
        pass

    def query(self, query: str, *, limit: int, scope: str, query_kind: str | None = None):
        return ProviderQueryResponse(
            items=[{"document_id": "d1", "citation": "d1", "score": 1.0}],
            cost_usd=0.0,
        )

    def close(self) -> None:
        pass


class _Manifest:
    def __init__(self) -> None:
        self.corpus = _Corpus()
        self.queries = [
            _Query("q1", "s1", [_Evidence("d1", 1.0)]),
            _Query("q2", "s1", [_Evidence("d1", 1.0)]),
        ]
        self.providers = [
            type("P", (), {"id": "p1", "vendor_reported_only": False})(),
        ]


class _Evidence:
    def __init__(self, document_id: str, weight: float) -> None:
        self.document_id = document_id
        self.weight = weight


class _Corpus:
    documents = []


class _Query:
    def __init__(self, text: str, scope: str, evidence) -> None:
        self.id = text
        self.query = text
        self.scope = scope
        self.kind = "temporal"
        self.answerable = False
        self.expected_evidence = evidence


def test_derive_on_query_false_forces_derive_false() -> None:
    adapter = _DeriveAdapter()
    runner = BenchmarkRunner(manifest=_Manifest(), adapters={"p1": adapter},
                             allow_missing=True, derive_on_query=False)
    runner._run_provider("p1")  # noqa: SLF001
    assert adapter.calls == [False, False], (
        f"severian-like adapter must receive derive=False on every scored "
        f"query, got {adapter.calls}"
    )


def test_plain_adapter_never_receives_derive_kwarg() -> None:
    adapter = _PlainAdapter()
    runner = BenchmarkRunner(manifest=_Manifest(), adapters={"p1": adapter},
                             allow_missing=True, derive_on_query=False)
    # Should not raise TypeError: no derive kwarg is injected.
    runner._run_provider("p1")  # noqa: SLF001


def test_default_derive_on_query_preserves_legacy_behaviour() -> None:
    adapter = _DeriveAdapter()
    runner = BenchmarkRunner(manifest=_Manifest(), adapters={"p1": adapter},
                             allow_missing=True)
    runner._run_provider("p1")  # noqa: SLF001
    assert adapter.calls == [None, None], (
        "legacy default must not force derive=False (only opt-out forces it)"
    )
