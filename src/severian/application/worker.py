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

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from severian.application.derivation import DerivationGateway, DerivationService
from severian.application.dreaming import (
    ConsolidationService,
    ContradictionScanService,
    CurationService,
    RetentionPolicyService,
)
from severian.application.episodes import EpisodeService
from severian.application.ports import CanonicalRepository
from severian.application.projections import ProjectionCoordinator
from severian.domain.errors import NotFoundError
from severian.domain.models import Event, Job, Scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerSummary:
    processed: int = 0
    failed: int = 0
    touched_scopes: tuple[Scope, ...] = ()

    @property
    def total(self) -> int:
        return self.processed + self.failed


class DerivationWorker:
    """Processes pending derive/episode/dream jobs and keeps projections in sync.

    The derivation service may be None for dream-only operation (a worker
    that only processes dream/episode jobs). A derive job arriving when
    derivation is None raises a clear RuntimeError instead of a bare
    AttributeError on NoneType.
    """

    def __init__(
        self,
        repository: CanonicalRepository,
        derivation: DerivationService | None,
        projection: ProjectionCoordinator,
        episodes: EpisodeService | None = None,
        consolidation: ConsolidationService | None = None,
        curation: CurationService | None = None,
        retention: RetentionPolicyService | None = None,
        contradictions: ContradictionScanService | None = None,
    ) -> None:
        self._repository = repository
        self._derivation = derivation
        self._projection = projection
        self._episodes = episodes if episodes is not None else EpisodeService(repository)
        self._consolidation = consolidation if consolidation is not None else ConsolidationService(repository)
        self._curation = curation if curation is not None else CurationService(repository)
        self._retention = retention if retention is not None else RetentionPolicyService(repository)
        self._contradictions = contradictions if contradictions is not None else ContradictionScanService(repository)

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

        JOB-01: expired leases are reaped at the start of every pass so a
        crashed worker's ``running`` jobs are re-queued (or dead-lettered
        once attempts are exhausted) as part of normal runtime operation —
        the reaper is wired in, not dead code.
        """
        try:
            self._repository.reap_expired_jobs()
        except Exception:
            logger.warning("reap_expired_jobs failed at drain start", exc_info=True)
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
                    logger.warning("failed to mark job %s as failed: %s", job.id, exc, exc_info=True)
                failed += 1
        # Converge projections for every touched scope BEFORE any
        # completed job becomes durable. zero-debt 3.2: project the
        # outbox delta INCREMENTALLY (apply), never a scope-wide
        # clear+reinsert — the outbox is the projection delta contract
        # and every canonical write already appended its event.
        applied_events = self._project_outbox(touched)
        for job in pending_done:
            self._repository.complete_job(job.id, "done", worker_id=worker_id)
        # OPS-01: the outbox projector. Ack only events whose projections
        # converged above — ONE transaction via ack_outbox_many (zero-debt
        # 3.3; the drain must not commit per event). Ack is idempotent —
        # a crash between apply and ack leaves the event pending and the
        # next pass re-processes it.
        if applied_events:
            try:
                self._repository.ack_outbox_many([e.id for e in applied_events])
            except Exception:
                logger.warning("failed to ack outbox batch", exc_info=True)
        return WorkerSummary(
            processed=processed,
            failed=failed,
            touched_scopes=tuple(touched.values()),
        )

    def _project_outbox(self, touched: dict[str, Scope]) -> list[Event]:
        """Apply pending outbox events for touched scopes incrementally.

        The outbox is the projection delta contract (zero-debt 3.2):
        each pending event is applied as a bounded upsert/delete, never
        a scope-wide clear+reinsert. Events for scopes NOT touched this
        pass remain pending (a later pass that touches them will apply
        them). Returns the applied events so the caller can ack them
        only after the projections converged (crash between apply and
        ack → event stays pending → next pass re-applies, idempotent).
        """
        if not touched:
            return []
        touched_set = set(touched.keys())
        pending = [e for e in self._repository.pending_outbox() if e.scope.model_dump_json() in touched_set]
        if pending:
            self._projection.apply(pending)
        return pending

    def _derive(
        self,
        job: Job,
        gateway: DerivationGateway,
        policy: Mapping[str, Any] | None,
    ) -> None:
        if job.kind == "derive":
            if self._derivation is None:
                raise RuntimeError(
                    "derive job requires a derivation service, but worker has "
                    "derivation=None (dream-only mode); cannot process derive jobs"
                )
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
        if job.kind == "dream":
            mode = str(job.payload.get("mode", "consolidate"))
            if mode == "consolidate":
                self._consolidation.consolidate(job.scope)
            elif mode == "curate":
                self._curation.curate(job.scope)
            elif mode == "retention":
                policy = dict(job.payload.get("policy", {}) or {})
                self._retention.evaluate(job.scope, policy)
            elif mode == "retention-archive":
                policy = dict(job.payload.get("policy", {}) or {})
                self._retention.apply_archive(job.scope, policy)
            elif mode == "contradictions":
                self._contradictions.scan(job.scope)
            else:
                raise ValueError(f"unsupported dream mode: {mode}")
            return
        raise ValueError(f"unsupported job kind: {job.kind}")
