"""Tests for the provider-benchmark manifest and runner.

The benchmark contract is part of Phase 3 acceptance: the user
explicitly required Severian to be compared against at least two
external memory providers before release. This test ensures the
manifest format is versioned, validated, and the runner can compare
Severian against stub external adapters through the same query set.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from severian.benchmarks.manifest import (
    BenchmarkManifest,
    ManifestValidationError,
    load_manifest,
    validate_manifest,
)
from severian.benchmarks.runner import BenchmarkRunner, ProviderAdapter, ProviderQueryResponse


def _minimal_manifest() -> dict[str, object]:
    return {
        "schema_version": 1,
        "manifest_id": "bench-2026-07-31-001",
        "name": "phase3-smoke",
        "corpus": {
            "id": "corpus-1",
            "version": "v1",
            "seed": 42,
            "documents": [
                {"id": "d1", "content": "Ada founded a company.", "scope": "t1/p1/c1/a1/s1"},
                {"id": "d2", "content": "An old fact.", "scope": "t1/p1/c1/a1/s1", "is_stale": True},
                {"id": "d3", "content": "A current fact.", "scope": "t1/p1/c1/a1/s1"},
                {"id": "d4", "content": "Other scope fact.", "scope": "other/p1/c1/a1/s1"},
            ],
        },
        "queries": [
            {
                "id": "q1",
                "kind": "fact",
                "query": "Who founded a company?",
                "scope": "t1/p1/c1/a1/s1",
                "expected_evidence": [{"document_id": "d1", "weight": 1.0}],
            },
            {
                "id": "q2",
                "kind": "temporal",
                "query": "What is current?",
                "scope": "t1/p1/c1/a1/s1",
                "expected_evidence": [{"document_id": "d3", "weight": 1.0}],
            },
            {
                "id": "q3",
                "kind": "negative",
                "query": "What is impossible?",
                "scope": "t1/p1/c1/a1/s1",
                "answerable": False,
                "expected_evidence": [],
            },
            {
                "id": "q4",
                "kind": "adversarial",
                "query": "Scope-safe fact",
                "scope": "t1/p1/c1/a1/s1",
                "expected_evidence": [{"document_id": "d1", "weight": 1.0}],
            }
        ],
        "providers": [
            {
                "id": "severian",
                "kind": "severian",
                "backend": "sqlite",
                "model": {"id": "local/hasher", "prompt_version": "v1"},
                "config": {"dimension": 256},
            },
            {
                "id": "hosted",
                "kind": "external",
                "vendor": "HostedCo",
                "deployment": "hosted",
                "model": {"id": "hosted/embed-v3", "prompt_version": "v1"},
                "config": {"api_base": "https://api.hosted.example"},
            },
            {
                "id": "selfhosted",
                "kind": "external",
                "vendor": "OssCorp",
                "deployment": "self-hosted",
                "model": {"id": "oss/embed-v2", "prompt_version": "v1"},
                "config": {"endpoint": "http://localhost:8080"},
            },
        ],
        "metrics": [
            "recall_at_10",
            "mrr",
            "ndcg_at_10",
            "citation_precision",
            "stale_fact_rate",
            "temporal_accuracy",
            "cross_scope_leakage",
            "unsupported_answer_rate",
            "latency_p95_ms",
            "cost_per_query_usd",
        ],
        "release_gate": {
            "min_recall_at_10": 0.95,
            "min_citation_precision": 1.0,
            "max_stale_fact_rate": 0.0,
            "max_cross_scope_leakage": 0.0,
            "max_unsupported_answer_rate": 0.0,
            "require_external_providers": 2,
        },
    }


def test_manifest_round_trip(tmp_path: Path):
    payload = _minimal_manifest()
    manifest_path = tmp_path / "bench.json"
    manifest_path.write_text(json.dumps(payload))
    manifest = load_manifest(manifest_path)
    assert manifest.manifest_id == "bench-2026-07-31-001"
    assert len(manifest.providers) == 3
    assert manifest.release_gate.require_external_providers == 2


def test_manifest_rejects_unknown_schema_version(tmp_path: Path):
    payload = _minimal_manifest()
    payload["schema_version"] = 99
    manifest_path = tmp_path / "bench.json"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(ManifestValidationError, match="schema_version"):
        load_manifest(manifest_path)


def test_manifest_rejects_invalid_metric(tmp_path: Path):
    payload = _minimal_manifest()
    payload["metrics"] = ["totally_made_up_metric"]
    manifest_path = tmp_path / "bench.json"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(ManifestValidationError, match="metric"):
        load_manifest(manifest_path)


def test_manifest_requires_two_external_providers(tmp_path: Path):
    payload = _minimal_manifest()
    # Keep only severian
    payload["providers"] = [payload["providers"][0]]
    manifest_path = tmp_path / "bench.json"
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(ManifestValidationError, match="external"):
        load_manifest(manifest_path)


def test_validate_manifest_passes_for_minimal():
    manifest = BenchmarkManifest.model_validate(_minimal_manifest())
    assert validate_manifest(manifest) is manifest


def test_runner_executes_queries_through_each_provider():
    payload = _minimal_manifest()
    manifest = BenchmarkManifest.model_validate(payload)

    @dataclass
    class FakeExternal(ProviderAdapter):
        provider_id: str
        documents: list[tuple[str, str]]

        def ingest(self, document_id: str, content: str, *, scope: str) -> None:
            self.documents.append((document_id, content))

        def query(self, query: str, *, limit: int, scope: str):
            document_id = "d3" if query == "What is current?" else "d1"
            if query == "What is impossible?":
                return ProviderQueryResponse(items=[], cost_usd=0.01)
            return ProviderQueryResponse(
                items=[{"document_id": document_id, "citation": document_id, "score": 0.5}],
                cost_usd=0.01,
            )

        def close(self) -> None:
            return None

    @dataclass
    class FakeSeverian(ProviderAdapter):
        documents: list[tuple[str, str]]

        def ingest(self, document_id: str, content: str, *, scope: str) -> None:
            self.documents.append((document_id, content))

        def query(self, query: str, *, limit: int, scope: str):
            document_id = "d3" if query == "What is current?" else "d1"
            if query == "What is impossible?":
                return ProviderQueryResponse(items=[], cost_usd=0.01)
            return ProviderQueryResponse(
                items=[{"document_id": document_id, "citation": document_id, "score": 1.0}],
                cost_usd=0.01,
            )

        def close(self) -> None:
            return None

    severian = FakeSeverian(documents=[])
    hosted = FakeExternal(provider_id="hosted", documents=[("d1", "Ada founded a company.")])
    selfhosted = FakeExternal(provider_id="selfhosted", documents=[("d1", "Ada founded a company.")])
    adapters = {"severian": severian, "hosted": hosted, "selfhosted": selfhosted}

    runner = BenchmarkRunner(manifest=manifest, adapters=adapters)
    result = runner.run()

    assert set(result.results.keys()) == {"severian", "hosted", "selfhosted"}
    severian_result = result.results["severian"]
    assert severian_result.recall_at_10 == 1.0
    assert severian_result.citation_precision == 1.0
    assert severian_result.cross_scope_leakage == 0.0
    assert severian_result.temporal_accuracy == 1.0
    assert severian_result.unsupported_answer_rate == 0.0
    assert severian_result.cost_per_query_usd == 0.01
    assert result.gate_passed is True

    # Both external adapters should produce evidence and respect the
    # same query set; recall is computed against the same expected
    # evidence list so the runner is the same code path for every
    # provider.
    for provider_id in ("hosted", "selfhosted"):
        external = result.results[provider_id]
        assert external.queries_run == 4
        assert external.recall_at_10 == 1.0
        assert external.citation_precision == 1.0


def test_runner_computes_stale_temporal_scope_and_unsupported_metrics():
    manifest = BenchmarkManifest.model_validate(_minimal_manifest())

    @dataclass
    class AdversarialAdapter(ProviderAdapter):
        closed: bool = False

        def ingest(self, document_id: str, content: str, *, scope: str) -> None:
            return None

        def query(self, query: str, *, limit: int, scope: str) -> ProviderQueryResponse:
            if query == "Who founded a company?":
                item = {"document_id": "d2", "citation": "d2"}
            elif query == "What is current?":
                item = {"document_id": "d4", "citation": "d4"}
            elif query == "What is impossible?":
                item = {"document_id": "d1", "citation": "d1"}
            else:
                item = {"document_id": "d4", "citation": "d4"}
            return ProviderQueryResponse(items=[item], cost_usd=0.25)

        def close(self) -> None:
            self.closed = True

    adapters = {provider.id: AdversarialAdapter() for provider in manifest.providers}
    result = BenchmarkRunner(manifest=manifest, adapters=adapters).run()

    measured = result.results["severian"]
    assert measured.stale_fact_rate == 0.25
    assert measured.temporal_accuracy == 0.0
    assert measured.cross_scope_leakage == 0.5
    assert measured.unsupported_answer_rate == 0.25
    assert measured.cost_per_query_usd == 0.25
    assert result.gate_passed is False
    assert "severian" in result.gate_failures
    assert adapters["severian"].closed is True


def test_runner_rejects_response_without_explicit_cost():
    manifest = BenchmarkManifest.model_validate(_minimal_manifest())

    @dataclass
    class MissingCostAdapter(ProviderAdapter):
        def ingest(self, document_id: str, content: str, *, scope: str) -> None:
            return None

        def query(self, query: str, *, limit: int, scope: str):
            return [{"document_id": "d1"}]

        def close(self) -> None:
            return None

    with pytest.raises(ValueError, match="ProviderQueryResponse"):
        BenchmarkRunner(
            manifest=manifest,
            adapters={provider.id: MissingCostAdapter() for provider in manifest.providers},
        ).run()
