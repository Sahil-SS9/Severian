"""P0-D RED: metric denominators are population-correct.

The audited gate divided answerability failures by ALL queries, so a
lane that failed every unanswerable probe still passed (26/120 =
0.2167 < 0.22 ceiling), and citation precision averaged unanswerable
failures into answerable quality. These tests pin the corrected
semantics:

- answerable-query citation precision denominator = answerable only;
- unanswerable false-positive/failed-abstention denominator =
  unanswerable only;
- a fixture returning results for every negative query reports a
  negative false-positive rate of 1.0 — never N_unanswerable/N_total;
- perfect answerable retrieval does NOT mask complete failure on the
  unanswerable population.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pytest

from severian.benchmarks.manifest import BenchmarkManifest
from severian.benchmarks.runner import (
    BenchmarkRunner,
    ProviderAdapter,
    ProviderQueryResponse,
)

# Query texts follow "service handles {N} requests" with N = i*7, so an
# adapter can deterministically recover the expected document id d{i}
# from the query text alone (the harness passes text, not ids).
_QUERY_RE = re.compile(r"service handles (\d+) requests")


def _answerable_doc(query: str) -> str | None:
    match = _QUERY_RE.search(query)
    if match is None:
        return None
    return f"d{int(match.group(1)) // 7}"


def _manifest(n_answerable: int = 94, n_unanswerable: int = 26) -> BenchmarkManifest:
    """n_answerable fact queries + n_unanswerable negative/adversarial
    queries. Query kinds cover the REQUIRED_QUERY_KINDS contract
    (temporal/negative/adversarial) so the manifest is valid schema v2."""
    documents = [
        {"id": f"d{i}", "content": f"fact {i}: the service handles {i*7} requests per second",
         "scope": "t1/p1/c1/a1/s1"}
        for i in range(n_answerable)
    ]
    queries = [
        {"id": f"q{i}", "kind": "temporal" if i == 0 else "fact",
         "query": f"service handles {i*7} requests",
         "scope": "t1/p1/c1/a1/s1", "answerable": True,
         "expected_evidence": [{"document_id": f"d{i}", "weight": 1.0}]}
        for i in range(n_answerable)
    ] + [
        {"id": f"neg{i}", "kind": "adversarial" if i % 2 else "negative",
         "query": f"totally unrelated topic {i}",
         "scope": "t1/p1/c1/a1/s1", "answerable": False, "expected_evidence": []}
        for i in range(n_unanswerable)
    ]
    payload: dict[str, Any] = {
        "schema_version": 2,
        "manifest_id": "denominator-red",
        "name": "denominator",
        "corpus": {"id": "c1", "version": "1", "seed": 1, "documents": documents},
        "queries": queries,
        "providers": [
            {"id": "severian", "kind": "severian", "deployment": "local",
             "backend": "sqlite", "model": {"id": "hash", "prompt_version": "v1"},
             "config": {}},
        ],
        "metrics": ["recall_at_10", "mrr", "ndcg_at_10", "citation_precision",
                    "stale_fact_rate", "temporal_accuracy", "cross_scope_leakage",
                    "unsupported_answer_rate", "negative_false_positive_rate",
                    "latency_p95_ms", "cost_per_query_usd"],
        "release_gate": {"min_recall_at_10": 0.9, "min_citation_precision": 0.78,
                         "max_stale_fact_rate": 0.0, "max_cross_scope_leakage": 0.0,
                         "max_unsupported_answer_rate": 0.1,
                         "max_negative_false_positive_rate": 0.0,
                         "require_external_providers": 0},
    }
    return BenchmarkManifest.model_validate(payload)


@dataclass
class AlwaysReturnAdapter(ProviderAdapter):
    """Returns the right evidence for answerable queries AND a result
    for EVERY unanswerable query — the always-returning retrieval path
    the audit caught neutralising abstention safety."""

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        return None

    def query(self, query: str, *, limit: int, scope: str,
              query_kind: str | None = None) -> ProviderQueryResponse:
        # Answerable queries: perfect top-1. Unanswerable queries:
        # WRONGLY returns a result (false positive).
        doc = _answerable_doc(query)
        item = {"document_id": doc if doc is not None else "d0",
                "citation": "x"}
        return ProviderQueryResponse(items=[item], cost_usd=0.0)

    def close(self) -> None:
        return None


def test_always_returning_lane_reports_fp_rate_of_one() -> None:
    """P0-D: failing ALL 26 unanswerable probes must yield 1.0, never
    26/120 = 0.2167. The old diluted rate let this lane pass the 0.22
    ceiling."""
    manifest = _manifest(n_answerable=94, n_unanswerable=26)
    result = BenchmarkRunner(manifest=manifest, adapters={"severian": AlwaysReturnAdapter()}).run()
    measured = result.results["severian"]
    assert measured.negative_false_positive_rate == 1.0
    assert measured.unsupported_answer_rate == 0.0
    # Perfect answerable retrieval must NOT mask abstention failure.
    assert measured.citation_precision == 1.0
    assert measured.recall_at_10 == 1.0
    assert result.gate_passed is False
    assert "negative_false_positive_rate" in result.gate_failures["severian"]


def test_citation_precision_denominator_is_answerable_only() -> None:
    """Perfect answerable retrieval with complete negative failure keeps
    citation precision at 1.0 — the unanswerable population contributes
    ONLY to negative_false_positive_rate (never averaged into citation
    precision as the old 94/120 = 0.7833 coupling did)."""
    manifest = _manifest(n_answerable=94, n_unanswerable=26)
    measured = (
        BenchmarkRunner(manifest=manifest, adapters={"severian": AlwaysReturnAdapter()})
        .run()
        .results["severian"]
    )
    assert measured.citation_precision == 1.0
    assert measured.negative_false_positive_rate == 1.0
    assert measured.citation_precision + measured.negative_false_positive_rate == 2.0


def test_fp_rate_denominator_scales_with_unanswerable_count() -> None:
    """A lane returning results for every negative probe reports 1.0 at
    ANY unanswerable population size — the rate is population-correct,
    not diluted by answerable query count."""
    for n_unanswerable in (2, 5, 26, 40):
        manifest = _manifest(n_answerable=94, n_unanswerable=n_unanswerable)
        measured = BenchmarkRunner(
            manifest=manifest, adapters={"severian": AlwaysReturnAdapter()}
        ).run().results["severian"]
        assert measured.negative_false_positive_rate == 1.0, n_unanswerable


@dataclass
class PerfectAbstainingAdapter(ProviderAdapter):
    """Correct: right evidence for answerable, EMPTY for unanswerable."""

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        return None

    def query(self, query: str, *, limit: int, scope: str,
              query_kind: str | None = None) -> ProviderQueryResponse:
        doc = _answerable_doc(query)
        if doc is not None:
            return ProviderQueryResponse(
                items=[{"document_id": doc, "citation": doc}], cost_usd=0.0
            )
        return ProviderQueryResponse(items=[], cost_usd=0.0)

    def close(self) -> None:
        return None


def test_perfect_abstaining_lane_passes_gate() -> None:
    manifest = _manifest(n_answerable=94, n_unanswerable=26)
    result = BenchmarkRunner(
        manifest=manifest, adapters={"severian": PerfectAbstainingAdapter()}
    ).run()
    measured = result.results["severian"]
    assert measured.negative_false_positive_rate == 0.0
    assert measured.unsupported_answer_rate == 0.0
    assert measured.citation_precision == 1.0
    assert result.gate_passed is True


@dataclass
class AbstainOnAnswerableAdapter(ProviderAdapter):
    """Fails every answerable query (empty) but abstains correctly on
    negatives: unsupported_answer_rate must be 1.0 over the ANSWERABLE
    population, never 94/120."""

    def ingest(self, document_id: str, content: str, *, scope: str, supersedes: Iterable[str] = ()) -> None:
        return None

    def query(self, query: str, *, limit: int, scope: str,
              query_kind: str | None = None) -> ProviderQueryResponse:
        return ProviderQueryResponse(items=[], cost_usd=0.0)

    def close(self) -> None:
        return None


def test_answerable_abstention_denominator_is_answerable_only() -> None:
    manifest = _manifest(n_answerable=94, n_unanswerable=26)
    measured = BenchmarkRunner(
        manifest=manifest, adapters={"severian": AbstainOnAnswerableAdapter()}
    ).run().results["severian"]
    # 94/94 answerable queries returned nothing -> 1.0, not 94/120.
    assert measured.unsupported_answer_rate == 1.0
    assert measured.recall_at_10 == 0.0
    assert measured.negative_false_positive_rate == 0.0


def test_runner_fails_loudly_if_populations_disappear_at_runtime() -> None:
    """Even if a manifest bypasses validation, the runner must never
    return a convenient 0.0/1.0 for an undefined population."""
    manifest = _manifest(n_answerable=94, n_unanswerable=26)
    runner = BenchmarkRunner(manifest=manifest, adapters={"severian": AlwaysReturnAdapter()})
    # Simulate a manifest whose queries all became answerable post-load.
    runner._manifest.queries = [q for q in manifest.queries if q.answerable]
    with pytest.raises(RuntimeError, match="no unanswerable queries"):
        runner._run_provider("severian")
