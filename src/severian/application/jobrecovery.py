"""Job recovery (0.6.3): putting jobs that ended ``failed`` or ``dead_letter`` back in the queue.

``severian jobs requeue --kind K [--status failed|dead_letter] [--transient] [--confirm]`` plans with
``plan_requeue`` (dry run, nothing written) and carries the plan out with ``requeue``.

On 21/11 a long outage of the local facts model left 451 ``extract_facts`` jobs ``failed`` for good;
``requeue_job`` only takes ``dead_letter`` rows, so they were reset by hand with SQL. The rules both
backends share for a bulk requeue live here so they cannot drift apart: what a requeued job looks
like, and what its audit row may say (ids and counts, never the error text: it can quote a reply).
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from severian.domain.models import Job

REQUEUEABLE_JOB_STATUSES = ("failed", "dead_letter")

# A job error that says the model server (or the network to it) was down, busy or slow: trying again later
# can work. Matched on the reason only, never on the quoted reply after " (reply: ", which is model text.
# The 21/11 live failures, in the extractor's wording: "<urlopen error [Errno 111] Connection refused>",
# "HTTP Error 503: Service Unavailable" (no_backend / Loading model), "timed out", "HTTP Error 409: Conflict".
_TRANSIENT = re.compile(
    r"HTTP Error (?:409|429|5\d\d)\b|urlopen error|\[Errno (?:32|101|104|110|111|113)\]|connection (?:refused|reset|"
    r"aborted)|timed out|remote ?end closed|remotedisconnected|broken pipe|incompleteread|no route to host|"
    r"network is unreachable",
    re.IGNORECASE,
)
_REPLY = " (reply: "


def requeue_reset(job: Job) -> Job:
    """The job as it re-enters the queue: pending, a fresh attempt budget, no owner, lease or wait.

    ``last_error`` is kept, as ``requeue_job`` keeps it, so the earlier failure can still be read.
    """
    return job.model_copy(
        update={
            "status": "pending",
            "attempts": 0,
            "worker_id": None,
            "lease_until": None,
            "retry_after": None,
            "dead_letter": False,
        }
    )


def requeue_audit(job: Job, from_status: str) -> dict[str, Any]:
    """The ``job.requeue`` audit metadata: the kind, the state it left and the attempts it had."""
    return {"kind": job.kind, "from_status": from_status, "attempts": job.attempts}


def is_transient_error(error: str | None) -> bool:
    """True when a job's error says the server was away, busy or slow; never for a reply problem.

    An unreadable or cut-off reply (temperature 0) fails the same way again, so it is not requeued by
    ``--transient``; only the reason is read, never the reply text quoted after it.
    """
    if not error:
        return False
    reason = error.split(_REPLY, 1)[0]
    return _TRANSIENT.search(reason) is not None


class _JobStore(Protocol):
    def list_jobs(self, *, status: str, kind: str | None = None) -> list[Job]: ...
    def requeue_jobs(self, job_ids: Sequence[str], *, from_status: str = "failed") -> list[Job]: ...


@dataclass(frozen=True)
class RequeuePlan:
    """Which jobs a requeue would take. ``skipped_other_errors``: same kind and state, other error class."""

    kind: str
    status: str
    transient_only: bool
    job_ids: tuple[str, ...]
    skipped_other_errors: int

    @property
    def matched(self) -> int:
        return len(self.job_ids)


def plan_requeue(repository: _JobStore, *, kind: str, status: str = "failed", transient_only: bool) -> RequeuePlan:
    """The jobs of ``kind`` in ``status`` to put back; with ``transient_only`` only server faults. Writes nothing."""
    if status not in REQUEUEABLE_JOB_STATUSES:
        raise ValueError("only failed or dead_letter jobs can be requeued")
    jobs = repository.list_jobs(status=status, kind=kind)
    chosen = [job for job in jobs if not transient_only or is_transient_error(job.last_error)]
    return RequeuePlan(
        kind=kind,
        status=status,
        transient_only=transient_only,
        job_ids=tuple(job.id for job in chosen),
        skipped_other_errors=len(jobs) - len(chosen),
    )


def requeue(repository: _JobStore, plan: RequeuePlan) -> list[Job]:
    """Carry a plan out: one transaction, one ``job.requeue`` audit row per job (see ``requeue_jobs``)."""
    return repository.requeue_jobs(plan.job_ids, from_status=plan.status)
