"""Pydantic request/response models for the versioned HTTP API.

All scope fields are mandatory on every request: there is no implicit
scope on the server. The model rejects empty scope dimensions, which
mirrors the domain invariant in :class:`severian.domain.models.Scope`.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ScopeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str

    @classmethod
    def from_domain(cls, scope: Any) -> ScopeModel:
        return cls(
            tenant_id=scope.tenant_id,
            profile_id=scope.profile_id,
            collection_id=scope.collection_id,
            agent_id=scope.agent_id,
            session_id=scope.session_id,
        )

    @field_validator(
        "tenant_id", "profile_id", "collection_id", "agent_id", "session_id"
    )
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("scope dimensions must not be empty")
        return value


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ScopeModel
    content: str = Field(min_length=1, max_length=1_000_000)
    source: dict[str, Any] = Field(default_factory=dict)


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str
    content: str
    content_hash: str
    scope: ScopeModel
    source: dict[str, Any]
    job_id: str
    job_kind: str


class ObservationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_id: str
    content: str
    content_hash: str
    scope: ScopeModel
    source: dict[str, Any]
    created_at: str


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ScopeModel
    query: str = Field(min_length=1, max_length=10_000)
    limit: int = Field(default=20, ge=1, le=200)
    derive: bool = False


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    content: str
    source_observation_ids: list[str]
    lexical_score: float
    vector_score: float
    fused_score: float
    scope: ScopeModel


class MemoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    content: str
    kind: str
    source_observation_ids: list[str]
    scope: ScopeModel
    created_at: str


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hits: list[SearchHit]


class HealthComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: str
    detail: str = ""


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    components: dict[str, str]
