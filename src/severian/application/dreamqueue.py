"""The dreaming producer: who queues a dream job, and the switch that lets any of it happen.

Before this module nothing queued a ``dream`` job. ``DerivationWorker._derive`` handled the five
modes if a row happened to exist, and the CLI, the API and the scripts never wrote one — the whole
dreaming pass was reachable only by hand-written SQL. That is why "repeated dream runs duplicate
facts" was never observed: there were no repeated runs.

Two things wake it up, both behind ``SEVERIAN_DREAMING`` (off by default):

- the operator command ``severian dream --mode <m> <scope flags>`` (dry run unless ``--confirm``);
- the worker tick ``severian work --dream-tenant T --dream-profile P``, which queues one job per
  (active scope, tick mode) and then lets the worker's dream pass run them.

The tick does NOT queue ``retention``/``retention-archive``: pruning stays an explicit operator
action (S2 decision 5). It also cannot queue anything twice — ``enqueue_job`` is idempotent on
(kind, payload), so a second tick returns the same job rows and the queue converges. A mode that
has already run stays queued-out until its payload changes, which is the honest reading of "the
tick is a tick": to dream a store again on purpose, use the operator command.

With the switch off nothing here runs, no job is excluded from the derive drain, and
``search``/``assemble_context``/``drain`` behave exactly as they do at 0.4.1.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from severian.application.ports import CanonicalRepository
from severian.domain.models import Job, Scope

#: The job kind the worker claims in its dream pass.
DREAM_JOB = "dream"
#: Every mode ``_derive`` understands.
DREAM_MODES = ("consolidate", "curate", "retention", "retention-archive", "contradictions")
#: What the tick queues by default: the non-destructive, model-free passes.
DEFAULT_TICK_MODES = ("consolidate", "curate", "contradictions")
_TRUE = {"1", "true", "yes", "on"}


def dreaming_enabled(env: Mapping[str, str] | None = None) -> bool:
    """``SEVERIAN_DREAMING`` — the switch for every Should behaviour of this item, off by default."""
    values = os.environ if env is None else env
    return values.get("SEVERIAN_DREAMING", "").strip().lower() in _TRUE


def dream_background_kinds(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Job kinds the derive drain must leave alone.

    Only while the switch is on: with it off, ``drain`` keeps taking dream jobs exactly as it did
    at 0.4.1 (a behaviour a manual ``enqueue_job`` could always rely on), and with it on the dream
    jobs belong to the dream pass, so the derive drain must not swallow them.
    """
    return (DREAM_JOB,) if dreaming_enabled(env) else ()


@dataclass(frozen=True)
class QueueResult:
    """What one tick or one operator queue call saw, for the log and the test that pins it."""

    scopes: int = 0
    modes: tuple[str, ...] = field(default_factory=tuple)
    jobs: tuple[str, ...] = field(default_factory=tuple)

    def summary(self) -> dict[str, Any]:
        return {"scopes": self.scopes, "modes": list(self.modes), "jobs": list(self.jobs)}


class DreamProducer:
    """Queues dream jobs for scopes. Idempotent by construction (see the module docstring)."""

    def __init__(
        self,
        repository: CanonicalRepository,
        *,
        modes: Sequence[str] = DEFAULT_TICK_MODES,
        policy: Mapping[str, Any] | None = None,
    ) -> None:
        unknown = [mode for mode in modes if mode not in DREAM_MODES]
        if unknown:
            raise ValueError(f"unknown dream mode(s): {', '.join(unknown)}")
        self._repository = repository
        self._modes = tuple(modes)
        self._policy = dict(policy or {})

    def queue_scope(self, scope: Scope) -> list[Job]:
        """One job per mode for ``scope``; an existing job is returned, never duplicated."""
        jobs: list[Job] = []
        for mode in self._modes:
            payload: dict[str, Any] = {"mode": mode}
            if self._policy and mode in ("retention", "retention-archive"):
                payload["policy"] = dict(self._policy)
            jobs.append(self._repository.enqueue_job(scope, DREAM_JOB, payload))
        return jobs

    def queue_profile(self, tenant_id: str, profile_id: str) -> QueueResult:
        """Queue every active scope of one profile (the scopes the store actually holds turns for)."""
        scopes = self._repository.list_profile_scopes(tenant_id, profile_id)
        jobs: list[Job] = []
        for scope in scopes:
            jobs.extend(self.queue_scope(scope))
        return QueueResult(
            scopes=len(scopes),
            modes=self._modes,
            jobs=tuple(job.id for job in jobs),
        )


__all__ = [
    "DEFAULT_TICK_MODES",
    "DREAM_JOB",
    "DREAM_MODES",
    "DreamProducer",
    "QueueResult",
    "dream_background_kinds",
    "dreaming_enabled",
]
