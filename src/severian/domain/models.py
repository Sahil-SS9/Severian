from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


def now() -> datetime:
    return datetime.now(UTC)


# ``superseded_by_id`` of an erasure tombstone (memory or observation): the
# marker is itself superseded, so it is never an active head, never pending
# derivation and never projected, while history keeps it for audit.
ERASED = ":erased"
# Predicates that hold one current value per subject: a newer value ends the
# older one (kept in history with valid_to). Everything else accumulates.
SINGLE_VALUED_PREDICATES = frozenset({"works_at", "located_in", "based_in"})


class TruthState(StrEnum):
    """Where a stored statement stands in time (Step 1 of the next-improvements plan).

    ``current``     the latest word on the subject.
    ``historical``  was true, then replaced by a newer, certain statement; kept, never deleted.
    ``transition``  the change itself: a newer statement that replaced an older one.
    ``concurrent``  several values are true together (\"open Mondays\" and \"open Fridays\").
    """

    CURRENT = "current"
    HISTORICAL = "historical"
    TRANSITION = "transition"
    CONCURRENT = "concurrent"


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


class Attachment(BaseModel):
    """Structured metadata for a multi-modal attachment (Tier 3-2).

    The canonical store holds only the reference + digest, never the
    binary payload. ``kind`` is image|audio|video|file|transcript;
    ``uri`` points at the original evidence (file path, URL, or object
    key); ``media_type`` is the MIME type when known; ``digest`` is a
    SHA-256 of the attachment bytes (deterministic, verifiable);
    ``alt_text`` or ``transcript`` carries the text description that
    feeds derivation/retrieval.
    """

    kind: str = Field(pattern=r"^(image|audio|video|file|transcript)$")
    uri: str = Field(min_length=1)
    media_type: str = ""
    digest: str = ""
    alt_text: str = ""
    transcript: str = ""
    size_bytes: int = 0

    @field_validator("digest")
    @classmethod
    def _digest_is_sha256_or_empty(cls, value: str) -> str:
        if value == "":
            return value
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("digest must be a 64-char lowercase hex SHA-256 digest")
        return value


class Observation(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    # ``content`` is the DERIVED, normalised form used for search and
    # derivation. ``raw_content`` preserves the immutable evidence as it
    # was received; ``raw_hash`` is the SHA-256 of the raw bytes. The
    # raw pair is never rewritten by ingestion or revision.
    content: str = Field(min_length=1)
    content_hash: str = ""
    raw_content: str = ""
    raw_hash: str = ""
    source: dict[str, Any] = Field(default_factory=dict)
    # Multi-modal attachments (Tier 3-2): structured metadata for
    # non-text evidence (image/audio/video/file). The attachment content
    # itself is NOT stored in the canonical store — only its reference
    # and a content digest, so the store stays text-first and
    # deterministic. Derivation/retrieval operate on ``content`` (the
    # text description or transcript), while ``attachments`` preserves
    # the pointer to the original evidence.
    attachments: list[Attachment] = Field(default_factory=list)
    revision_of_id: str | None = None
    superseded_by_id: str | None = None
    created_at: datetime = Field(default_factory=now)

    @field_validator("content_hash", "raw_hash")
    @classmethod
    def _hash_is_sha256_or_empty(cls, value: str) -> str:
        # A hash field is either empty (not yet computed/verified) or a
        # 64-char lowercase hex SHA-256 digest. Rejecting malformed
        # hashes at the boundary keeps forged or truncated hashes from
        # ever entering the canonical store.
        if value == "":
            return value
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("hash must be a 64-char lowercase hex SHA-256 digest")
        return value


class Trigger(BaseModel):
    """When an instruction-for-later should come back (Step 4): the situations that match it.

    ``tools`` / ``paths`` / ``repos`` match the current tool, file path or repository exactly;
    ``keywords`` are stemmed words that must mostly appear in the current message.
    """

    tools: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    repos: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.tools or self.paths or self.repos or self.keywords)


class Memory(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    scope: Scope
    kind: str
    content: str = Field(min_length=1)
    source_observation_ids: list[str] = Field(min_length=1)
    model_id: str
    prompt_version: str
    confidence: float = Field(ge=0, le=1)
    revision_of_id: str | None = None
    superseded_by_id: str | None = None
    created_at: datetime = Field(default_factory=now)
    # Event time (Step 3): the period ``[occurred_at, occurred_until)`` the memory says the thing
    # happened in (UTC), found by rules from the text. ``created_at`` stays "when it was saved".
    # Both None when the text names no clear time.
    occurred_at: datetime | None = None
    occurred_until: datetime | None = None
    # Anticipation (Step 4). ``cues``: short phrases saying when this memory will be needed (likely
    # future questions, situations), indexed for search but never shown as facts. ``trigger``: set on
    # instructions for later ("next time X, do Y"). Both stay empty unless anticipation is switched on.
    cues: list[str] = Field(default_factory=list)
    trigger: Trigger | None = None
    # Truth resolution (handoff 1, SEVERIAN_TRUTH_RESOLUTION): set when a newer fact replaced this one.
    # ``valid_until`` is when the newer message was written; ``replaced_by_id`` is the newer memory.
    # The memory stays in the store as history and is never deleted; both stay None unless switched on.
    valid_until: datetime | None = None
    replaced_by_id: str | None = None
    # Usage tracking (Tier 2 salience): number of retrieval-time hits
    # recorded for this memory. Feeds the salience blend in curation and
    # the optional retrieval decay. Deterministic; incremented via
    # ``record_memory_usage``.
    usage_count: int = 0


def revision_lineage(memories: dict[str, Memory], memory_id: str) -> list[Memory]:
    """Every memory in ``memory_id``'s revision family, oldest first.

    The family is everything linked by ``revision_of_id``: walk back to the
    root, then collect all descendants. Following ``superseded_by_id`` instead
    loses any head that rollback demoted (its forward pointer is cleared), and
    an erasure tombstone stays listed for audit. Cycle-safe.
    """
    root = memories[memory_id]
    visited: set[str] = set()
    while root.revision_of_id is not None and root.revision_of_id in memories and root.id not in visited:
        visited.add(root.id)
        root = memories[root.revision_of_id]
    children: dict[str, list[Memory]] = {}
    for memory in memories.values():
        if memory.revision_of_id is not None:
            children.setdefault(memory.revision_of_id, []).append(memory)
    family: dict[str, Memory] = {}
    stack = [root]
    while stack:
        memory = stack.pop()
        if memory.id in family:
            continue
        family[memory.id] = memory
        stack.extend(children.get(memory.id, []))
    return sorted(family.values(), key=lambda memory: (memory.created_at, memory.id))


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
    # Belief lineage, kept apart from world-time validity: the observations
    # a derived relation was extracted from. When the last of them is
    # revised or erased, ``retracted_at`` is set and the relation leaves the
    # current graph; history keeps it. Manually added relations have no
    # lineage and are never retracted by source corrections.
    source_observation_ids: list[str] = Field(default_factory=list)
    retracted_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)

    def supported_by(self, observation_id: str | None) -> Relation | None:
        """This relation with ``observation_id`` added to its lineage; None when unchanged.

        A retracted relation is reinstated with that single support. A
        relation without lineage (legacy or manual) is left as it is: its
        support is unknown, so one source's correction must never retract it.
        """
        if observation_id is None:
            return None
        if self.retracted_at is not None:
            return self.model_copy(update={"source_observation_ids": [observation_id], "retracted_at": None})
        if not self.source_observation_ids or observation_id in self.source_observation_ids:
            return None
        return self.model_copy(update={"source_observation_ids": [*self.source_observation_ids, observation_id]})

    def unsupported_by(self, observation_id: str, *, at: datetime) -> Relation | None:
        """This relation without ``observation_id`` in its lineage; None when it never cited it.

        Losing its last support retracts it as of ``at``.
        """
        if observation_id not in self.source_observation_ids:
            return None
        lineage = [source for source in self.source_observation_ids if source != observation_id]
        retracted_at = self.retracted_at if lineage else (self.retracted_at or at)
        return self.model_copy(update={"source_observation_ids": lineage, "retracted_at": retracted_at})

    def replaced_by(self, newer: Relation, *, at: datetime) -> Relation | None:
        """This relation ended as of ``at`` because ``newer`` gives a new value; None when unaffected.

        Only single-valued predicates (one current value per subject) are
        replaced: same subject and predicate, different object, still open.
        """
        if (
            self.predicate not in SINGLE_VALUED_PREDICATES
            or self.id == newer.id
            or self.subject_id != newer.subject_id
            or self.predicate != newer.predicate
            or self.object_id == newer.object_id
            or self.valid_to is not None
        ):
            return None
        valid_to = at if self.valid_from is None or at >= self.valid_from else self.valid_from
        return self.model_copy(update={"valid_to": valid_to})

    @property
    def truth_state(self) -> TruthState:
        """``historical`` once a newer value ended it (``valid_to`` set), else ``current``."""
        return TruthState.HISTORICAL if self.valid_to is not None else TruthState.CURRENT

    def reopened(self) -> Relation | None:
        """This relation made current again when restated after being replaced; None when already open."""
        return None if self.valid_to is None else self.model_copy(update={"valid_to": None})

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
