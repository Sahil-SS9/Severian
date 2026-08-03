"""Concrete provider adapters for the Severian benchmark harness.

Each adapter implements the two-method protocol from
:class:`severian.benchmarks.runner.ProviderAdapter`:

- ``ingest(document_id, content, *, scope)``
- ``query(query, *, limit, scope) -> ProviderQueryResponse``
- ``close()``

Adapters never embed external SDKs at import time; provider SDKs are
imported lazily inside methods so the core package, unit tests, and
manifest validation work without the optional dependency.
"""
from __future__ import annotations

from severian.benchmarks.adapters.baseline import LexicalBaselineAdapter
from severian.benchmarks.adapters.chroma import ChromaAdapter
from severian.benchmarks.adapters.severian import SeverianAdapter

__all__ = [
    "ChromaAdapter",
    "LexicalBaselineAdapter",
    "SeverianAdapter",
]
