"""Job recovery (0.6.3): putting jobs that ended ``failed`` or ``dead_letter`` back in the queue.

On 21/11 a long outage of the local facts model left 451 ``extract_facts`` jobs ``failed`` for good;
``requeue_job`` only takes ``dead_letter`` rows, so they were reset by hand with SQL. The rules both
backends share for a bulk requeue live here so they cannot drift apart: what a requeued job looks
like, and what its audit row may say (ids and counts, never the error text: it can quote a reply).
"""
from __future__ import annotations

from typing import Any

from severian.domain.models import Job

REQUEUEABLE_JOB_STATUSES = ("failed", "dead_letter")


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

