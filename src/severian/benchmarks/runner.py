"""Deterministic provider benchmark runner.

Every adapter receives the same corpus and scoped query set. Required
metrics are calculated from returned evidence and explicit adapter cost
metadata; the runner never substitutes optimistic constants for missing
semantics.
"""
from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from severian.benchmarks.manifest import BenchmarkManifest


@dataclass(frozen=True)
class ProviderQueryResponse:
    items: list[dict[str, Any]]
    cost_usd: float


@dataclass
class ProviderResult:
    provider_id: str
    queries_run: int
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    citation_precision: float
    stale_fact_rate: float
    temporal_accuracy: float
    cross_scope_leakage: float
    unsupported_answer_rate: float
    latency_p95_ms: float
    cost_per_query_usd: float
    raw: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BenchmarkResult:
    manifest_id: str
    results: dict[str, ProviderResult]
    gate_passed: bool
    gate_failures: dict[str, list[str]] = field(default_factory=dict)
    skipped_lanes: list[str] = field(default_factory=list)


class ProviderAdapter(Protocol):
    """Adapter interface every benchmark provider must implement."""

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None: ...
    def query(self, query: str, *, limit: int, scope: str, query_kind: str | None = None) -> ProviderQueryResponse: ...
    def close(self) -> None: ...


class BenchmarkRunner:
    """Run and gate the benchmark across every registered provider adapter."""

    def __init__(
        self,
        *,
        manifest: BenchmarkManifest,
        adapters: Mapping[str, ProviderAdapter],
        allow_missing: bool = False,
    ) -> None:
        live_providers = [provider for provider in manifest.providers if not provider.vendor_reported_only]
        missing = [provider.id for provider in live_providers if provider.id not in adapters]
        if missing and not allow_missing:
            raise ValueError(f"missing adapters for providers: {missing}")
        self._manifest = manifest
        self._adapters = dict(adapters)
        self._missing = missing
        self._documents = {document.id: document for document in manifest.corpus.documents}

    def _ingest(self, adapter: ProviderAdapter) -> None:
        for document in self._manifest.corpus.documents:
            adapter.ingest(
                document.id,
                document.content,
                scope=document.scope,
                supersedes=document.supersedes,
            )

    @staticmethod
    def _recall_at_k(expected: Iterable[str], returned_ids: list[str], k: int = 10) -> float:
        expected_set = set(expected)
        if not expected_set:
            return 0.0
        hits = len(set(returned_ids[:k]) & expected_set)
        return hits / len(expected_set)

    @staticmethod
    def _mrr(expected: Iterable[str], returned_ids: list[str]) -> float:
        expected_set = set(expected)
        for index, item in enumerate(returned_ids, start=1):
            if item in expected_set:
                return 1.0 / index
        return 0.0

    @staticmethod
    def _ndcg_at_k(expected_weights: Mapping[str, float], returned_ids: list[str], k: int = 10) -> float:
        unique_returned_ids = list(dict.fromkeys(returned_ids))
        dcg = sum(
            expected_weights.get(item, 0.0) / math.log2(index + 1)
            for index, item in enumerate(unique_returned_ids[:k], start=1)
        )
        ideal = sum(
            weight / math.log2(index + 1)
            for index, weight in enumerate(sorted(expected_weights.values(), reverse=True)[:k], start=1)
        )
        return dcg / ideal if ideal > 0 else 0.0

    @staticmethod
    def _validate_response(response: ProviderQueryResponse) -> ProviderQueryResponse:
        if not isinstance(response, ProviderQueryResponse):
            raise ValueError(
                "provider adapter must return ProviderQueryResponse with explicit cost_usd; "
                "missing metadata would make the benchmark unverifiable"
            )
        if not math.isfinite(response.cost_usd) or response.cost_usd < 0:
            raise ValueError("provider response cost_usd must be finite and non-negative")
        if not isinstance(response.items, list):
            raise ValueError("provider response items must be a list")
        if any(not isinstance(item, dict) for item in response.items):
            raise ValueError("provider response items must be dictionaries")
        if any(not isinstance(item.get("document_id"), str) or not item["document_id"] for item in response.items):
            raise ValueError("every provider response item must contain a non-empty document_id")
        return response

    def _run_provider(self, provider_id: str) -> ProviderResult:
        adapter = self._adapters[provider_id]
        recalls: list[float] = []
        mrrs: list[float] = []
        ndcgs: list[float] = []
        temporal_scores: list[float] = []
        citation_precisions: list[float] = []
        latencies_ms: list[float] = []
        total_cost = 0.0
        returned_total = 0
        stale_total = 0
        leakage_total = 0
        unsupported_total = 0
        raw: list[dict[str, Any]] = []
        try:
            self._ingest(adapter)
            for query in self._manifest.queries:
                expected_ids = [evidence.document_id for evidence in query.expected_evidence]
                expected_weights = {evidence.document_id: evidence.weight for evidence in query.expected_evidence}
                started = time.perf_counter()
                response = self._validate_response(
                    adapter.query(query.query, limit=10, scope=query.scope, query_kind=query.kind)
                )
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                total_cost += response.cost_usd
                latencies_ms.append(elapsed_ms)
                returned_ids = [str(item.get("document_id", "")) for item in response.items]
                returned_total += len(returned_ids)
                # Rank-aware citation precision: a provider is precise when
                # the top ``expected_count`` items it returns ARE the
                # expected evidence. Raw precision over a fixed limit=10
                # would cap every provider at ~0.1, hiding real differences.
                expected_count = len(expected_ids)
                if expected_count == 0:
                    citation_precisions.append(1.0 if not returned_ids else 0.0)
                else:
                    top_expected_window = returned_ids[:expected_count]
                    citation_precisions.append(
                        len(set(expected_ids) & set(top_expected_window)) / expected_count
                    )
                stale_total += sum(
                    1
                    for document_id in returned_ids
                    if self._documents.get(document_id) is not None
                    and self._documents[document_id].is_stale
                    # Temporal probes explicitly ask for the OLD fact —
                    # the expected evidence IS a stale doc by design.
                    # Counting it as a stale-fact error would penalise
                    # correct historical recall. The stale gate measures
                    # whether CURRENT facts leak stale superseded ones,
                    # which is only meaningful on non-temporal queries.
                    and query.kind != "temporal"
                )
                leakage_total += sum(
                    1
                    for document_id in returned_ids
                    if self._documents.get(document_id) is None
                    or self._documents[document_id].scope != query.scope
                )
                if query.kind == "temporal":
                    expected_set = set(expected_ids)
                    temporal_scores.append(
                        len(expected_set & set(returned_ids)) / len(expected_set) if expected_set else 0.0
                    )
                has_answer = bool(returned_ids)
                if (query.answerable and not has_answer) or (not query.answerable and has_answer):
                    unsupported_total += 1
                if query.answerable:
                    recalls.append(self._recall_at_k(expected_ids, returned_ids))
                    mrrs.append(self._mrr(expected_ids, returned_ids))
                    ndcgs.append(self._ndcg_at_k(expected_weights, returned_ids))
                raw.append(
                    {
                        "query_id": query.id,
                        "scope": query.scope,
                        "returned_ids": returned_ids,
                        "citations": [str(item.get("citation", "")) for item in response.items],
                        "elapsed_ms": elapsed_ms,
                        "cost_usd": response.cost_usd,
                    }
                )
        finally:
            adapter.close()
        latencies_ms.sort()
        p95_index = max(0, int(round(0.95 * (len(latencies_ms) - 1))))
        return ProviderResult(
            provider_id=provider_id,
            queries_run=len(self._manifest.queries),
            recall_at_10=sum(recalls) / len(recalls),
            mrr=sum(mrrs) / len(mrrs),
            ndcg_at_10=sum(ndcgs) / len(ndcgs),
            citation_precision=sum(citation_precisions) / len(citation_precisions) if citation_precisions else 1.0,
            stale_fact_rate=stale_total / returned_total if returned_total else 0.0,
            temporal_accuracy=sum(temporal_scores) / len(temporal_scores),
            cross_scope_leakage=leakage_total / returned_total if returned_total else 0.0,
            unsupported_answer_rate=unsupported_total / len(self._manifest.queries),
            latency_p95_ms=latencies_ms[p95_index] if latencies_ms else 0.0,
            cost_per_query_usd=total_cost / len(self._manifest.queries),
            raw=raw,
        )

    def _evaluate_gate(self, results: dict[str, ProviderResult]) -> dict[str, list[str]]:
        failures: dict[str, list[str]] = {}
        gate = self._manifest.release_gate
        # The release gate measures the platform under test (kind ==
        # "severian"). External/competitor lanes are compared and
        # reported, but their failures never block Severian's release
        # gate — the strategy's success criteria require Severian to
        # pass, not the competition.
        platform_ids = {provider.id for provider in self._manifest.providers if provider.kind == "severian"}
        for provider_id, result in results.items():
            if provider_id not in platform_ids:
                continue
            provider_failures: list[str] = []
            if result.recall_at_10 < gate.min_recall_at_10:
                provider_failures.append("recall_at_10")
            if result.citation_precision < gate.min_citation_precision:
                provider_failures.append("citation_precision")
            if result.stale_fact_rate > gate.max_stale_fact_rate:
                provider_failures.append("stale_fact_rate")
            if result.cross_scope_leakage > gate.max_cross_scope_leakage:
                provider_failures.append("cross_scope_leakage")
            if result.unsupported_answer_rate > gate.max_unsupported_answer_rate:
                provider_failures.append("unsupported_answer_rate")
            if provider_failures:
                failures[provider_id] = provider_failures
        external_count = sum(
            1 for provider in self._manifest.providers if provider.kind == "external" and provider.id in results
        )
        if external_count < gate.require_external_providers:
            failures["__manifest__"] = ["external_provider_count"]
        return failures

    def run(self) -> BenchmarkResult:
        live = [provider for provider in self._manifest.providers if not provider.vendor_reported_only]
        results = {provider.id: self._run_provider(provider.id) for provider in live if provider.id in self._adapters}
        failures = self._evaluate_gate(results)
        # Lanes skipped for environmental reasons (e.g. chroma venv not
        # installed) are recorded visibly in skipped_lanes, never as a
        # silent pass AND never as a platform gate failure: a missing
        # competitor lane must not block Severian's own release gate.
        return BenchmarkResult(
            manifest_id=self._manifest.manifest_id,
            results=results,
            gate_passed=not failures,
            gate_failures=failures,
            skipped_lanes=list(self._missing),
        )


def to_jsonable(result: BenchmarkResult) -> dict[str, Any]:
    return {
        "manifest_id": result.manifest_id,
        "gate_passed": result.gate_passed,
        "gate_failures": result.gate_failures,
        "skipped_lanes": result.skipped_lanes,
        "results": {key: asdict(value) for key, value in result.results.items()},
    }


__all__ = [
    "BenchmarkResult",
    "BenchmarkRunner",
    "ProviderAdapter",
    "ProviderQueryResponse",
    "ProviderResult",
    "to_jsonable",
]
