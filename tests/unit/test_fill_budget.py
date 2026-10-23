"""SEVERIAN_FILL_BUDGET: fill the character budget instead of stopping at the result count.

Severian returns top-10 even when the items are short and most of the space is empty (LoCoMo: 1.6k characters
returned at an 8,000-character budget). With the switch on, search over-fetches a bounded pool and assembly keeps
adding ranked items until the character budget is full. A tight budget behaves exactly as today (the budget runs out
inside the first ``limit`` items), and with the switch off nothing changes at all.
"""
from __future__ import annotations

import pytest

from severian.application import fillbudget
from severian.application.fillbudget import fill_budget_enabled, pool_limit


def test_switch_off_by_default() -> None:
    assert fill_budget_enabled() is False
    assert pool_limit(10) == 10


@pytest.mark.parametrize("value", ["1", "true", "on", "yes", "TRUE", " On "])
def test_switch_reads_the_same_values_as_the_other_switches(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_FILL_BUDGET", value)
    assert fill_budget_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no", "maybe"])
def test_switch_ignores_everything_else(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_FILL_BUDGET", value)
    assert fill_budget_enabled() is False


def test_pool_is_bounded_and_never_below_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_FILL_BUDGET", "1")
    for limit in (1, 5, 10, 20, 39, fillbudget.POOL_CAP):
        pool = pool_limit(limit)
        assert pool >= limit, "the pool must never return fewer candidates than the caller asked for"
        assert pool <= fillbudget.POOL_CAP, "search latency is gated: the pool has a hard cap"
    assert pool_limit(10) > 10, "with the switch on a normal limit over-fetches"
    assert pool_limit(fillbudget.POOL_CAP) == fillbudget.POOL_CAP
    assert pool_limit(fillbudget.POOL_CAP + 1) == fillbudget.POOL_CAP + 1, "past the cap the caller's ask wins"


def test_pool_limit_refuses_a_nonsense_limit() -> None:
    with pytest.raises(ValueError):
        pool_limit(0)
