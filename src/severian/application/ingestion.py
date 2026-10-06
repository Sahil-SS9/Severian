from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from severian.application.ports import CanonicalRepository
from severian.domain.errors import NotFoundError
from severian.domain.models import Attachment, Collection, Job, Observation, Scope

# Source-identity keys probed (in order) when building the idempotency
# identity for an observation. The identity must be STABLE across
# re-ingests of the same evidence, and DIFFERENT for evidence from
# different sources even when the normalised content collides (P0-1).
_SOURCE_IDENTITY_KEYS = ("uri", "source_id", "idempotency_key")


@dataclass(frozen=True)
class IngestionResult:
    observation: Observation
    job: Job


def normalize_content(content: str) -> str:
    normalized = unicodedata.normalize("NFKC", content)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip()
    if not normalized:
        raise ValueError("observation content must not be empty")
    return normalized


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


def raw_hash_of(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class IngestionService:
    def __init__(self, repository: CanonicalRepository):
        self.repository = repository

    def ingest(
        self,
        scope: Scope,
        content: str,
        source: dict[str, Any],
        attachments: list[Attachment] | None = None,
        *,
        also_queue: Sequence[str] = (),
    ) -> IngestionResult:
        # Preserve the RAW evidence exactly as received; normalisation is
        # a derived field stored alongside it. Both survive in the
        # observation so no source content is ever lost (P0-1). The
        # canonical write and its derive-job enqueue are ONE transaction
        # (P0-4): a crash cannot leave an observation that never
        # derives, or a job that references nothing.
        # DATA-05: ensure the scope's collection exists for this tenant
        # before writing. This defines the collection-ownership semantics:
        # a collection is auto-provisioned (idempotent) for the tenant on
        # first write, and a scope referencing an EXISTING collection owned
        # by a different tenant is rejected by the backend's ownership check.
        self._ensure_collection(scope)
        raw = content
        normalized = normalize_content(content)
        observation, job = self.repository.add_observation_and_enqueue(
            Observation(
                scope=scope,
                content=normalized,
                raw_content=raw,
                raw_hash=raw_hash_of(raw),
                source=dict(source),
                attachments=list(attachments or []),
            ),
            also_queue=also_queue,
        )
        return IngestionResult(observation=observation, job=job)

    def _ensure_collection(self, scope: Scope) -> None:
        """Idempotently create the scope's collection for its tenant.

        If a collection with the scope's collection_id ALREADY EXISTS (for
        any tenant), do not attempt creation — collection ids are globally
        unique; a duplicate insert would violate the PK. The backend's
        ownership check then raises if the existing collection belongs to a
        different tenant.
        """
        try:
            self.repository.get_collection(scope.tenant_id, scope.collection_id)
            return  # owned by this tenant — already provisioned
        except NotFoundError:
            pass
        # Does the id exist under ANY tenant? If so it belongs to someone
        # else — the write path will reject it. Do not try to create.
        if self.repository.collection_exists(scope.collection_id):
            return
        self.repository.create_collection(
            Collection(id=scope.collection_id, tenant_id=scope.tenant_id, name="auto")
        )
