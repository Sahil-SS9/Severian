"""Durable derivation worker.

The canonical write path enqueues a ``derive`` job for every ingested
observation (idempotent on scope + content). This worker closes the
documented Phase 3 gap: the job queue existed but nothing consumed it,
so observations were never promoted to memories and default search
returned empty.

The worker contract (P0-4 hardened):

- ``claim_job`` atomically transitions one pending job to ``running``
  under a lease; ``NotFoundError`` means an empty queue and ends the
  pass, while any other exception is a real fault and propagates
  (the queue is never silently treated as empty on storage errors).
- The worker derives the referenced observation into memories through
  the canonical ``DerivationService`` (never direct repository writes).
- A job is completed ``done`` ONLY AFTER its projection rebuild
  converges, so a crash mid-rebuild cannot leave an observation marked
  derived while retrieval still misses it. Completion is owner-checked
  (``worker_id``) and records the failure reason for diagnostics.
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
from severian.application.episodes import EpisodeService
from severian.application.ports import CanonicalRepository
from severian.application.projections import ProjectionCoordinator
from severian.domain.errors import NotFoundError
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
        episodes: EpisodeService | None = None,
    ) -> None:
        self._repository = repository
        self._derivation = derivation
        self._projection = projection
        self._episodes = episodes if episodes is not None else EpisodeService(repository)

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
        # Successful jobs are held open until their scope's projection
        # rebuild converges below; only then are they completed ``done``
        # (P0-4). A crash between derivation and rebuild leaves the job
        # ``running`` with an expiring lease → reaped and retried, never
        # falsely durable.
        pending_done: list[Job] = []
        while max_jobs is None or (processed + failed) < max_jobs:
            try:
                job = self._repository.claim_job(worker_id)
            except NotFoundError:
                break  # queue empty — normal end of pass
            scope_key = job.scope.model_dump_json()
            touched[scope_key] = job.scope
            try:
                self._derive(job, gateway, policy)
                pending_done.append(job)
                processed += 1
            except Exception as exc:
                # A failed job must not wedge the queue: mark it failed
                # with the error recorded and continue draining. The
                # leased-claim machinery lets a later reap retry it.
                try:
                    self._repository.complete_job(job.id, "failed", worker_id=worker_id, error=str(exc))
                except Exception:
                    pass
                failed += 1
        # Converge projections for every touched scope BEFORE any
        # completed job becomes durable.
        for scope in touched.values():
            self._projection.rebuild(scope)
        for job in pending_done:
            self._repository.complete_job(job.id, "done", worker_id=worker_id)
        return WorkerSummary(
            processed=processed,
            failed=failed,
            touched_scopes=tuple(touched.values()),
        )

    def _derive(
        self,
        job: Job,
        gateway: DerivationGateway,
        policy: Mapping[str, Any] | None,
    ) -> None:
        if job.kind == "derive":
            observation_id = str(job.payload.get("observation_id", ""))
            if not observation_id:
                raise ValueError("derive job missing observation_id")
            self._derivation.derive_observation(
                job.scope,
                observation_id,
                gateway,
                policy=dict(policy or {}),
            )
            return
        if job.kind == "episode":
            self._episodes.consolidate_session(job.scope, gateway=gateway, policy=dict(policy or {}))
            return
        raise ValueError(f"unsupported job kind: {job.kind}")
