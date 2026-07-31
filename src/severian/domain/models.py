from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def now() -> datetime:
    return datetime.now(UTC)


class Scope(BaseModel, frozen=True):
    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str

    @field_validator("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("scope dimensions must not be empty")
        return value


class Collection(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    tenant_id: str
    name: str
    policy: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=now)


class Observation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    content: str
    content_hash: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    revision_of_id: str | None = None
    superseded_by_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    kind: str
    content: str
    source_observation_ids: list[str] = Field(min_length=1)
    model_id: str
    prompt_version: str
    confidence: float = Field(ge=0, le=1)
    revision_of_id: str | None = None
    superseded_by_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class Entity(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    kind: str
    name: str
    created_at: datetime = Field(default_factory=now)


class Relation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    subject_id: str
    predicate: str
    object_id: str
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    created_at: datetime = Field(default_factory=now)

    @model_validator(mode="after")
    def valid_interval(self) -> Relation:
        if self.valid_from is not None and self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("valid_to must be greater than or equal to valid_from")
        return self


class Job(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    kind: str
    payload: dict[str, Any]
    status: str = "pending"
    worker_id: str | None = None
    created_at: datetime = Field(default_factory=now)


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    kind: str
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=now)


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    action: str
    target_id: str
    metadata: dict[str, Any]
    created_at: datetime = Field(default_factory=now)
