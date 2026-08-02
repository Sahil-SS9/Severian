from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def now() -> datetime:
    return datetime.now(UTC)


# Source-identity keys probed (in order) when building the idempotency
# identity for an observation. The identity must be STABLE across
# re-ingests of the same evidence, and DIFFERENT for evidence from
# different sources even when the normalised content collides (P0-1).
_SOURCE_IDENTITY_KEYS = ("uri", "source_id", "idempotency_key")


def source_identity(source: dict[str, Any]) -> str:
    """Stable identity of an evidence source.

    Returns the first non-empty value among the source-identity keys,
    or ``""`` when the source carries no identity. The empty string is
    treated as "no source identity" so legacy ingests (no source) keep
    deduping on content alone.
    """
    for key in _SOURCE_IDENTITY_KEYS:
        value = source.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


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
    # ``content`` is the DERIVED, normalised form used for search and
    # derivation. ``raw_content`` preserves the immutable evidence as it
    # was received; ``raw_hash`` is the SHA-256 of the raw bytes. The
    # raw pair is never rewritten by ingestion or revision.
    content: str
    content_hash: str = ""
    raw_content: str = ""
    raw_hash: str = ""
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
    attempts: int = 0
    lease_until: datetime | None = None
    last_error: str | None = None
    retry_after: datetime | None = None
    dead_letter: bool = False
    created_at: datetime = Field(default_factory=now)


class Event(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    kind: str
    payload: dict[str, Any]
    acked: bool = False
    created_at: datetime = Field(default_factory=now)


class AuditEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    action: str
    target_id: str
    metadata: dict[str, Any]
    created_at: datetime = Field(default_factory=now)
