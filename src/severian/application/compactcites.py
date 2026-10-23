"""``SEVERIAN_COMPACT_CITES``: one id per item instead of two, without losing the ability to resolve it.

Every assembled item spends two 36-character ids on its provenance: the memory id in the bracket and the source
observation ids on a trailing ``source:`` line, about 84 characters per item. At the 8,000-character Hermes budget
that is about 10%; at 1,500 characters it is over half the window. The compact form cites the source observation ids
in the bracket and drops the trailing line, costing about 39 characters per item instead of 84 — roughly 45 saved.

Provenance holds: ``severian_get_observation`` takes an observation id, and ``severian_correct`` / ``severian_forget``
accept one too, so every id the compact block cites resolves through the agent tools (tested end to end in
``tests/application/test_context_efficiency.py``). A memory with no source observation (an imported or synthesised
record) falls back to its memory id, so a block never cites nothing. Off by default.
"""
from __future__ import annotations

import os
from collections.abc import Sequence


def compact_cites_enabled() -> bool:
    """``SEVERIAN_COMPACT_CITES`` switches the short form on. Off: the 0.3.11 frames, byte for byte."""
    return os.environ.get("SEVERIAN_COMPACT_CITES", "").strip().lower() in {"1", "true", "on", "yes"}


def source_line(observation_ids: Sequence[str]) -> str:
    """The legacy suffix naming the source observations, or nothing when there are none."""
    return f"\nsource: {', '.join(observation_ids)}" if observation_ids else ""


def frame(memory_id: str, observation_ids: Sequence[str], *, compact: bool | None = None) -> tuple[str, str]:
    """The (prefix, suffix) around an item's text.

    ``compact`` False is the 0.3.11 frame. True cites the observation ids in the bracket and drops the suffix.
    None reads ``SEVERIAN_COMPACT_CITES``.
    """
    on = compact_cites_enabled() if compact is None else compact
    if not on:
        return f"[{memory_id}] ", source_line(observation_ids)
    cited = ",".join(observation_ids) if observation_ids else memory_id
    return f"[{cited}] ", ""


__all__ = ["compact_cites_enabled", "frame", "source_line"]
