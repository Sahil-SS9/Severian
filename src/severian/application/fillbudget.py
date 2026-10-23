"""``SEVERIAN_FILL_BUDGET``: fill the character budget instead of stopping at the result count.

Severian always hands over the top ``limit`` items, even when the items are short and most of the space is empty: on
LoCoMo the kept round-3 run averages 1.6k characters at an 8,000-character budget, so ten items fill a fifth of the
window. Going from 10 to 20 items on LoCoMo found the evidence for 6 more questions in 100 for about 1.4k extra
characters (``RETRIEVAL-H2H.md``). With this switch on, search over-fetches a bounded pool (``pool_limit``) and the
existing assembler keeps adding ranked items until the character budget is full, because it already lays blocks out
in rank order and stops when the budget is gone. A tight budget therefore behaves exactly as it does today: the
budget runs out inside the first ``limit`` items, so the extra candidates are never reached.

The pool is bounded on purpose: search latency is gated at 1.10x (or 10 ms) of 0.3.11, and the over-fetch multiplies
the work every search does. ``POOL_FACTOR`` and ``POOL_CAP`` are the only knobs, measured on the kept stores (see
``evidence/ctxeff/SPEED.md`` in the handoff evidence directory). Off by default.
"""
from __future__ import annotations

import os

#: How many candidates one search may fetch per item the caller asked for.
POOL_FACTOR = 4
#: The hard ceiling on candidates, whatever the caller asked for: this is what the latency gate is measured on.
POOL_CAP = 50


def fill_budget_enabled() -> bool:
    """``SEVERIAN_FILL_BUDGET`` switches the fill on. Off: every caller gets exactly the ``limit`` it asked for."""
    return os.environ.get("SEVERIAN_FILL_BUDGET", "").strip().lower() in {"1", "true", "on", "yes"}


def pool_limit(limit: int) -> int:
    """The number of candidates to fetch for a caller that wants ``limit`` items.

    Off, or already past the cap: exactly ``limit`` (no change to search). On: the larger of ``limit`` and
    ``POOL_FACTOR * limit``, capped at ``POOL_CAP``. Never below ``limit`` — a caller that already knows how many
    items it wants keeps getting at least that many before the budget decides.
    """
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not fill_budget_enabled():
        return limit
    return max(limit, min(POOL_CAP, POOL_FACTOR * limit))


__all__ = ["POOL_CAP", "POOL_FACTOR", "fill_budget_enabled", "pool_limit"]
