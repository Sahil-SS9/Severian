from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from severian.application.ports import CanonicalRepository
from severian.domain.models import Job, Observation, Scope


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


class IngestionService:
    def __init__(self, repository: CanonicalRepository):
        self.repository = repository

    def ingest(self, scope: Scope, content: str, source: dict[str, Any]) -> IngestionResult:
        observation = self.repository.add_observation(
            Observation(scope=scope, content=normalize_content(content), source=dict(source))
        )
        job = self.repository.enqueue_job(scope, "derive", {"observation_id": observation.id})
        return IngestionResult(observation=observation, job=job)
