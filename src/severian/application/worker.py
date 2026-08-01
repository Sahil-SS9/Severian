"""Durable derivation worker.

The canonical write path enqueues a ``derive`` job for every ingested
observation (idempotent on scope + content). This worker closes the
documented Phase 3 gap: the job queue existed but nothing consumed it,
so observations were never promoted to memories and default search
returned empty.

The worker contract:

- ``claim_job`` atomically transitions one pending job to ``running``.
- The worker derives the referenced observation into memories through
  the canonical ``DerivationService`` (never direct repository writes).
- It completes the job as ``done`` (or ``failed`` on error) so the
  queue is durable and replayable.
- Projections are rebuilt once per touched scope after a drain pass so
  retrieval sees the promoted memories.

A worker never writes to projection indexes or the repository itself;
it composes the same application services the API uses.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from severian.application.derivation import DerivationGateway, DerivationService
from severian.application.ports import CanonicalRepository
from severian.application.projections import ProjectionCoordinator
from severian.domain.models import Job, Scope


@dataclass(frozen=True)
class WorkerSummary:
    processed: int = 0
    failed: int = 0
    touched_scopes: tuple[Scope, ...] = ()

    @property
    def total(self) -> int:
        return self.processed + self.failed


class DerivationWorker:
    """Processes pending derive jobs and keeps projections in sync."""

    def __init__(
        self,
        repository: CanonicalRepository,
        derivation: DerivationService,
        projection: ProjectionCoordinator,
    ) -> None:
        self._repository = repository
        self._derivation = derivation
        self._projection = projection

    def drain(
        self,
        *,
        worker_id: str = "worker",
        gateway: DerivationGateway,
        policy: Mapping[str, Any] | None = None,
        max_jobs: int | None = None,
    ) -> WorkerSummary:
        """Process pending derive jobs until the queue is empty.

        ``max_jobs`` bounds the pass (useful for tests and throttled
        loops); ``None`` drains everything currently pending.
        """
        processed = 0
        failed = 0
        touched: dict[str, Scope] = {}
        while max_jobs is None or (processed + failed) < max_jobs:
            try:
                job = self._repository.claim_job(worker_id)
            except Exception:
                break
            scope_key = job.scope.model_dump_json()
            touched[scope_key] = job.scope
            try:
                self._process(job, gateway, policy)
                processed += 1
            except Exception:
                # A failed job must not wedge the queue: mark it failed
                # and continue draining the rest.
                try:
                    self._repository.complete_job(job.id, "failed")
                except Exception:
                    pass
                failed += 1
        for scope in touched.values():
            self._projection.rebuild(scope)
        return WorkerSummary(
            processed=processed,
            failed=failed,
            touched_scopes=tuple(touched.values()),
        )

    def _process(
        self,
        job: Job,
        gateway: DerivationGateway,
        policy: Mapping[str, Any] | None,
    ) -> None:
        if job.kind != "derive":
            raise ValueError(f"unsupported job kind: {job.kind}")
        observation_id = str(job.payload.get("observation_id", ""))
        if not observation_id:
            raise ValueError("derive job missing observation_id")
        self._derivation.derive_observation(
            job.scope,
            observation_id,
            gateway,
            policy=dict(policy or {}),
        )
        self._repository.complete_job(job.id, "done")
