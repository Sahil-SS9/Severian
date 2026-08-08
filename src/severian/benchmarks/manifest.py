"""Versioned provider-benchmark manifest and validator."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_SCHEMA_VERSION = 1
REQUIRED_METRICS: tuple[str, ...] = (
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
)
REQUIRED_QUERY_KINDS = {"temporal", "negative", "adversarial"}


class ManifestValidationError(ValueError):
    pass


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    prompt_version: str


class CorpusDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    content: str
    scope: str
    is_stale: bool = False
    # Document ids this document supersedes. The runner passes these to
    # adapters at ingest so a capable provider (Severian) can exercise
    # its correction/supersession machinery; naive providers ignore it
    # and are expected to fail the stale-fact gate.
    supersedes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_supersedes(self) -> CorpusDocument:
        if self.id in self.supersedes:
            raise ManifestValidationError(f"document {self.id!r} cannot supersede itself")
        return self


class CorpusSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    seed: int
    documents: list[CorpusDocument] = Field(min_length=1)


class ExpectedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_id: str
    weight: float = 1.0


class QuerySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal[
        "fact", "paraphrase", "multi_source", "correction", "temporal",
        "negative", "adversarial", "code", "crosslang", "long",
    ]
    query: str
    scope: str
    answerable: bool = True
    expected_evidence: list[ExpectedEvidence] = Field(default_factory=list)


class ProviderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["severian", "external"]
    vendor: str | None = None
    deployment: Literal["hosted", "self-hosted", "local"] | None = None
    backend: str | None = None
    model: ModelSpec
    config: dict[str, Any] = Field(default_factory=dict)
    # When True the provider is NOT run by the harness; its metrics are
    # recorded from the vendor's own publications/docs with a citation
    # (``config.reference_url`` required). Vendor-reported providers
    # never count toward the live release gate.
    vendor_reported_only: bool = False

    @model_validator(mode="after")
    def _require_external_deployment(self) -> ProviderSpec:
        if self.kind == "external" and not self.vendor_reported_only and self.deployment not in {
            "hosted",
            "self-hosted",
        }:
            raise ValueError("external providers require hosted or self-hosted deployment")
        if self.vendor_reported_only and not self.config.get("reference_url"):
            raise ValueError("vendor_reported_only providers require config.reference_url")
        return self


class ReleaseGate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_recall_at_10: float = 0.95
    min_citation_precision: float = 1.0
    max_stale_fact_rate: float = 0.0
    max_cross_scope_leakage: float = 0.0
    max_unsupported_answer_rate: float = 0.0
    require_external_providers: int = 2


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    manifest_id: str
    name: str
    corpus: CorpusSpec
    queries: list[QuerySpec] = Field(min_length=1)
    providers: list[ProviderSpec] = Field(min_length=1)
    metrics: list[str]
    release_gate: ReleaseGate

    @field_validator("schema_version")
    @classmethod
    def _check_schema(cls, value: int) -> int:
        if value != MANIFEST_SCHEMA_VERSION:
            raise ManifestValidationError(
                f"unsupported schema_version={value}; expected {MANIFEST_SCHEMA_VERSION}"
            )
        return value

    @model_validator(mode="after")
    def _check_required_metrics(self) -> BenchmarkManifest:
        missing = [metric for metric in REQUIRED_METRICS if metric not in self.metrics]
        if missing:
            raise ManifestValidationError(f"manifest missing required metrics: {missing}")
        return self

    @model_validator(mode="after")
    def _check_provider_counts(self) -> BenchmarkManifest:
        severian = [provider for provider in self.providers if provider.kind == "severian"]
        # Only LIVE externals count toward the release gate; vendor-reported
        # providers are informational and never satisfy the gate.
        externals = [
            provider for provider in self.providers if provider.kind == "external" and not provider.vendor_reported_only
        ]
        if not severian:
            raise ManifestValidationError("manifest must include at least one Severian provider")
        if len(externals) < self.release_gate.require_external_providers:
            raise ManifestValidationError(
                "manifest requires at least "
                f"{self.release_gate.require_external_providers} external providers; got {len(externals)}"
            )
        # The hosted+self-hosted distribution requirement only applies when
        # the gate asks for at least two live externals (the release
        # contract). A comparison manifest that runs a single self-hosted
        # lane and carries the rest as vendor-reported citations must be
        # validatable now, without an unreachable hosted requirement.
        if self.release_gate.require_external_providers >= 2:
            if not any(provider.deployment == "hosted" for provider in externals):
                raise ManifestValidationError("manifest must include at least one hosted external provider")
            if not any(provider.deployment == "self-hosted" for provider in externals):
                raise ManifestValidationError(
                    "manifest must include at least one self-hosted/open-source external provider"
                )
        return self

    @model_validator(mode="after")
    def _check_query_contract(self) -> BenchmarkManifest:
        document_ids = {document.id for document in self.corpus.documents}
        document_scopes = {document.scope for document in self.corpus.documents}
        if len(document_ids) != len(self.corpus.documents):
            raise ManifestValidationError("corpus document IDs must be unique")
        kinds = {query.kind for query in self.queries}
        missing_kinds = REQUIRED_QUERY_KINDS - kinds
        if missing_kinds:
            raise ManifestValidationError(f"manifest missing required query kinds: {sorted(missing_kinds)}")
        seen_query_ids: set[str] = set()
        for query in self.queries:
            if query.id in seen_query_ids:
                raise ManifestValidationError(f"query IDs must be unique: {query.id}")
            seen_query_ids.add(query.id)
            if query.scope not in document_scopes:
                raise ManifestValidationError(f"query scope has no corpus documents: {query.scope}")
            evidence_ids = {evidence.document_id for evidence in query.expected_evidence}
            unknown = evidence_ids - document_ids
            if unknown:
                raise ManifestValidationError(f"query {query.id} references unknown evidence: {sorted(unknown)}")
            if query.answerable and not query.expected_evidence:
                raise ManifestValidationError(f"answerable query {query.id} requires expected evidence")
            if not query.answerable and query.expected_evidence:
                raise ManifestValidationError(f"unanswerable query {query.id} must not require evidence")
        return self


def load_manifest(path: str | Path) -> BenchmarkManifest:
    """Load and validate a manifest from disk."""
    try:
        raw = json.loads(Path(path).read_text())
        return BenchmarkManifest.model_validate(raw)
    except ManifestValidationError:
        raise
    except Exception as exc:
        raise ManifestValidationError(str(exc)) from exc


def validate_manifest(manifest: BenchmarkManifest) -> BenchmarkManifest:
    """Idempotently re-validate an already-parsed manifest."""
    BenchmarkManifest.model_validate(manifest.model_dump())
    return manifest


BENCHMARK_REQUIRED_METRICS = REQUIRED_METRICS

__all__ = [
    "BENCHMARK_REQUIRED_METRICS",
    "BenchmarkManifest",
    "CorpusSpec",
    "ExpectedEvidence",
    "MANIFEST_SCHEMA_VERSION",
    "ManifestValidationError",
    "ModelSpec",
    "ProviderSpec",
    "QuerySpec",
    "ReleaseGate",
    "REQUIRED_METRICS",
    "load_manifest",
    "validate_manifest",
]
