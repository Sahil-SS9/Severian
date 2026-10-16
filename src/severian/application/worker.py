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
from severian.application.userfacts import BACKGROUND_JOB_KINDS, FACTS_JOB
from severian.domain.errors import NotFoundError
from severian.domain.models import Event, Job, Scope

logger = logging.getLogger(__name__)

# OPS-01b: upper bound on outbox events applied per idle backlog sweep.
_MAX_BACKLOG_EVENTS_PER_PASS = 500
# 16/10/2026: a busy local model (timeouts, "identical request already running", restarts) failed ~13% of fact
# jobs for good. A turn the model could not serve ONLY because the server was busy now waits and is tried again:
# 5, 10, 20, 40, 60 min apart (about 2 h 15 m in all), and fails with its reason after the last attempt.
FACTS_MAX_ATTEMPTS = 6
FACTS_RETRY_BASE_S = 300.0
FACTS_RETRY_MAX_S = 3600.0


def facts_retry_wait(attempts: int) -> float:
    """Seconds before a busy-model fact job is tried again, after its ``attempts``-th try."""
    return min(FACTS_RETRY_MAX_S, FACTS_RETRY_BASE_S * 2.0 ** max(0, attempts - 1))


@dataclass(frozen=True)
class WorkerSummary:
    processed: int = 0
    failed: int = 0
    touched_scopes: tuple[Scope, ...] = ()
    # Projection failures are isolated per pass (a transport outage must not
    # dead-letter derivations that already succeeded); this counter surfaces
    # them so callers and cron summaries can alarm on a stuck projection.
    projection_failed: int = 0
    # Round 2 idea 1: fact jobs the model could not serve (after its retry and split) are completed
    # as failed with the reason; counted apart from ``failed`` (storage faults) so callers can tell.
    gave_up: int = 0
    # Fact jobs sent back to wait for a busy model (FACTS_MAX_ATTEMPTS bounds it); not failures yet.
    deferred: int = 0

    @property
    def total(self) -> int:
        return self.processed + self.failed + self.gave_up


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
                # Background kinds (Round 2 idea 1 facts) have their own pass: saving never waits on them.
                job = self._repository.claim_job(worker_id, exclude_kinds=BACKGROUND_JOB_KINDS)
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
        #
        # A projection failure (e.g. embedding transport outage) must NOT
        # abort the pass: derivations already succeeded, so completing
        # those jobs is correct — the canonical records are durable and
        # their outbox events stay pending for a later pass to re-apply
        # idempotently. Aborting here instead left jobs ``running`` with
        # expired leases, which reaped into dead-letter (the 2026-08-20
        # fleet incident) while stranding every touched scope's outbox.
        applied_events = self._project_outbox(touched, worker_id)
        projection_failed = 1 if applied_events is None else 0
        applied_events = applied_events or []
        backlog_attempted = False
        # OPS-01b: idle-pass backlog sweep. Events stranded by earlier
        # failing passes belong to scopes whose jobs ALREADY completed;
        # the touched-scope rule above would never revisit them once the
        # queue drains (the 2026-08-22 incident left 258 such events).
        # An idle pass opportunistically applies pending events: apply()
        # is idempotent (upserts keyed by current memory head state) and
        # scope isolation is enforced at read time. The cap bounds the
        # per-pass APPLY size only — pending_outbox() itself fetches the
        # full pending list (bounded by the stranded backlog, which the
        # drain now converges every idle tick).
        if processed == 0 and failed == 0:
            # HG follow-up (2026-08-22): claim events atomically
            # (pending -> projecting with lease) so concurrent workers can
            # never apply the same event twice. Stale claims are returned
            # to pending by reap_expired_jobs.
            backlog = self._repository.claim_outbox(
                worker_id, limit=_MAX_BACKLOG_EVENTS_PER_PASS,
            )
            backlog = [e for e in backlog if e not in applied_events]
            backlog_attempted = bool(backlog)
            if backlog:
                converged = self._apply_batch(backlog)
                if converged:
                    applied_events = applied_events + converged
                if converged is None or len(converged) < len(backlog):
                    projection_failed = 1
                    # Release whatever did not converge (poison events
                    # isolated by bisection) so they retry next pass
                    # instead of holding projecting claims until lease
                    # expiry.
                    applied_ids = {e.id for e in (converged or [])}
                    stranded = [e for e in backlog if e.id not in applied_ids]
                    try:
                        self._repository.release_outbox([e.id for e in stranded])
                    except Exception:
                        logger.warning("failed to release outbox claims", exc_info=True)
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
        # OPS-01b alarm: after ack, any STILL-pending event that this pass
        # attempted means bisection isolated poison events — the alarm must
        # fire so the operator quarantines them; a silent partial is how
        # backlogs rot.
        if processed == 0 and failed == 0 and backlog_attempted:
            projection_failed = 1 if self._repository.pending_outbox() else projection_failed
        return WorkerSummary(
            processed=processed,
            failed=failed,
            touched_scopes=tuple(touched.values()),
            projection_failed=projection_failed,
        )

    def extract_facts(
        self,
        *,
        worker_id: str = "facts",
        batch_size: int = 8,
        max_batches: int | None = None,
    ) -> WorkerSummary:
        """Background pass for Round 2 idea 1 (SEVERIAN_USER_FACTS): queued turns -> user facts.

        Claims ``extract_facts`` jobs oldest first, ``batch_size`` at a time, so consecutive
        messages reach the model together; a batch is split by scope so no call mixes two users.
        Facts are projected before their jobs complete (same rule as ``drain``), so a completed
        job means its facts are searchable. Turns the model could not serve (after the extractor's
        retry and split) complete as failed with the reason and the start of the bad reply as the
        job's error (``gave_up``); they are not retried in a loop, and their raw turns were saved
        and made searchable by the normal pass. The exception is a turn that failed only because
        the model server was busy or down: it goes back to wait (``deferred``) and is tried again
        later, up to ``FACTS_MAX_ATTEMPTS`` tries in all (``facts_retry_wait``). ``max_batches`` bounds
        the pass; ``None`` empties
        the queue.
        """
        if self._derivation is None:
            raise RuntimeError("fact extraction needs a derivation service")
        processed = failed = gave_up = deferred = batches = projection_failed = 0
        touched_all: dict[str, Scope] = {}
        while max_batches is None or batches < max_batches:
            claimed: list[Job] = []
            while len(claimed) < batch_size:
                try:
                    claimed.append(self._repository.claim_job(worker_id, kind=FACTS_JOB))
                except NotFoundError:
                    break
            if not claimed:
                break
            batches += 1
            groups: dict[str, list[Job]] = {}
            for job in claimed:
                groups.setdefault(job.scope.model_dump_json(), []).append(job)
            done: list[Job] = []
            unserved: list[tuple[Job, str, bool]] = []
            touched: dict[str, Scope] = {}
            for key, jobs in groups.items():
                ids = [str(job.payload.get("observation_id", "")) for job in jobs]
                try:
                    result = self._derivation.derive_user_facts(jobs[0].scope, ids)
                except Exception as exc:  # a storage fault: fail these jobs, keep the pass going
                    for job in jobs:
                        try:
                            self._repository.complete_job(job.id, "failed", worker_id=worker_id, error=str(exc))
                        except Exception:
                            logger.warning("failed to mark job %s as failed: %s", job.id, exc, exc_info=True)
                    failed += len(jobs)
                    continue
                for job in jobs:
                    observation_id = str(job.payload.get("observation_id", ""))
                    reason = result.failed.get(observation_id)
                    if reason is None:
                        done.append(job)
                    else:
                        unserved.append((job, reason, observation_id in result.retryable))
                touched[key] = jobs[0].scope
            applied = self._project_outbox(touched, worker_id)
            if applied is None:
                projection_failed = 1
            for job in done:
                self._repository.complete_job(job.id, "done", worker_id=worker_id)
            for job, reason, busy in unserved:
                if busy and job.attempts < FACTS_MAX_ATTEMPTS:
                    self._repository.complete_job(
                        job.id, "failed", worker_id=worker_id, error=reason, retry_in=facts_retry_wait(job.attempts)
                    )
                    deferred += 1
                else:
                    self._repository.complete_job(job.id, "failed", worker_id=worker_id, error=reason)
                    gave_up += 1
            if applied:
                try:
                    self._repository.ack_outbox_many([event.id for event in applied])
                except Exception:
                    logger.warning("failed to ack outbox batch", exc_info=True)
            processed += len(done)
            touched_all.update(touched)
        return WorkerSummary(
            processed=processed,
            failed=failed,
            touched_scopes=tuple(touched_all.values()),
            projection_failed=projection_failed,
            gave_up=gave_up,
            deferred=deferred,
        )

    def _project_outbox(self, touched: dict[str, Scope], worker_id: str) -> list[Event] | None:
        """Apply pending outbox events for touched scopes incrementally.

        The outbox is the projection delta contract (zero-debt 3.2):
        each pending event is applied as a bounded upsert/delete, never
        a scope-wide clear+reinsert. Events for scopes NOT touched this
        pass remain pending (a later pass that touches them will apply
        them). Returns the applied events so the caller can ack them
        only after the projections converged (crash between apply and
        ack → event stays pending → next pass re-applies, idempotent).

        Returns ``None`` when projection failed outright: the caller
        completes the already-derived jobs (canonical records are
        durable) but acks nothing — events stay pending for retry.
        """
        if not touched:
            return []
        touched_set = set(touched.keys())
        # HG follow-up (2026-08-22): claim instead of peek. Only events
        # this worker atomically claimed (pending -> projecting) are
        # applied; a concurrent worker's claim cannot overlap.
        claimed = self._repository.claim_outbox(worker_id, limit=10_000)
        pending = [e for e in claimed if e.scope.model_dump_json() in touched_set]
        skipped = [e for e in claimed if e.scope.model_dump_json() not in touched_set]
        if skipped:
            # Not ours this pass: return them to pending immediately so a
            # later pass (or the sweep) can take them without waiting for
            # lease expiry.
            try:
                self._repository.release_outbox([e.id for e in skipped])
            except Exception:
                logger.warning("failed to release unclaimed outbox events", exc_info=True)
        if not pending:
            return []
        result = pending if self._apply_batch(pending) else None
        if result is None:
            # Batch failed: release claims so events retry next pass
            # rather than stalling until lease expiry.
            try:
                self._repository.release_outbox([e.id for e in pending])
            except Exception:
                logger.warning("failed to release outbox claims", exc_info=True)
        return result

    def _apply_batch(self, events: list[Event]) -> list[Event] | None:
        """Apply one bounded batch of outbox events to the projections.

        Shared by both projection paths (touched-scope delta and idle
        backlog sweep) so failure handling cannot drift between them.

        On a batch failure, bisects (then falls back to per-event) so ONE
        poison event cannot wedge the whole backlog behind it head-of-line
        — apply() is fail-fast, and the sweep's [:500] slice would other-
        wise retry the identical failing batch every idle pass forever.

        Returns the subset of ``events`` whose projection converged
        (caller acks exactly those); ``None`` means NOTHING applied and
        even isolation failed. Canonical data is safe on every path:
        failed events simply stay pending for later passes.
        """
        try:
            if not events:
                return []
            self._projection.apply(events)
            return events
        except Exception:
            if len(events) == 1:
                logger.warning(
                    "projection apply failed for event %s; leaving it pending",
                    events[0].id, exc_info=True,
                )
                return None
            logger.warning(
                "projection batch of %d failed; bisecting to isolate the "
                "poison event(s)", len(events), exc_info=True,
            )
        # Bisection: halves first, then single-event fallback. The good
        # half still converges; only genuinely poisoned events stay.
        mid = len(events) // 2
        applied: list[Event] = []
        left = self._apply_batch(events[:mid])
        if left is not None:
            applied.extend(left)
        right = self._apply_batch(events[mid:])
        if right is not None:
            applied.extend(right)
        return applied or None

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
