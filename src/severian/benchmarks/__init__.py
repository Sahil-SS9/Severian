"""Provider comparison benchmark for Severian.

The benchmark framework lets a release engineer compare Severian
against at least two external memory providers (one hosted, one
self-hosted/open-source where feasible) over the same query set
and report Recall@K, MRR, nDCG, citation precision, stale-fact
rate, temporal accuracy, cross-scope leakage, unsupported-answer
rate, latency and cost.

The release gate is configured in the manifest. Without execution
evidence the runner does not produce numbers; the contract forbids
copying benchmark results into source.
"""
from __future__ import annotations

from severian.benchmarks.manifest import (
    BenchmarkManifest,
    CorpusSpec,
    ExpectedEvidence,
    ManifestValidationError,
    ProviderSpec,
    QuerySpec,
    ReleaseGate,
    load_manifest,
    validate_manifest,
)
from severian.benchmarks.runner import (
    BenchmarkResult,
    BenchmarkRunner,
    ProviderAdapter,
    ProviderResult,
)

__all__ = [
    "BenchmarkManifest",
    "BenchmarkResult",
    "BenchmarkRunner",
    "CorpusSpec",
    "ExpectedEvidence",
    "ManifestValidationError",
    "ProviderAdapter",
    "ProviderResult",
    "ProviderSpec",
    "QuerySpec",
    "ReleaseGate",
    "load_manifest",
    "validate_manifest",
]
