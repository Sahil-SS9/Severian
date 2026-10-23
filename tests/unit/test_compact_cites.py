"""SEVERIAN_COMPACT_CITES: one id per item instead of two.

Each assembled item carries its memory id in the bracket and the source observation ids on a trailing ``source:``
line: about 84 characters per item at a 36-character id, roughly 10% of the 8,000-character budget and much more of
a 1,500-character one. The compact form cites the source observation ids only, in the bracket, and drops the
trailing line. Every id it cites resolves through the agent tools (``severian_get_observation`` takes an
observation id), so the compact block keeps the provenance promise while spending about 45 characters less per item.
"""
from __future__ import annotations

import pytest

from severian.application.compactcites import (
    compact_cites_enabled,
    frame,
    source_line,
)

MEM = "0f0e0d0c-1111-4222-8333-444455556666"
OBS = "aaabbbcc-1111-4222-8333-444455556666"
OBS2 = "dddeeeff-1111-4222-8333-444455556666"


def test_switch_off_by_default() -> None:
    assert compact_cites_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "on", "yes"])
def test_switch_on_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_COMPACT_CITES", value)
    assert compact_cites_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "off", "no"])
def test_switch_off_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SEVERIAN_COMPACT_CITES", value)
    assert compact_cites_enabled() is False


def test_off_keeps_the_existing_frames_byte_for_byte() -> None:
    assert frame(MEM, (OBS,), compact=False) == (f"[{MEM}] ", f"\nsource: {OBS}")
    assert frame(MEM, (OBS, OBS2), compact=False) == (f"[{MEM}] ", f"\nsource: {OBS}, {OBS2}")


def test_on_cites_the_observations_in_one_bracket_and_drops_the_source_line() -> None:
    assert frame(MEM, (OBS,), compact=True) == (f"[{OBS}] ", "")
    assert frame(MEM, (OBS, OBS2), compact=True) == (f"[{OBS},{OBS2}] ", "")


def test_on_falls_back_to_the_memory_id_when_there_is_no_source_observation() -> None:
    assert frame(MEM, (), compact=True) == (f"[{MEM}] ", "")


def test_compact_saves_the_expected_characters_per_item() -> None:
    before = sum(len(part) for part in frame(MEM, (OBS,), compact=False))
    after = sum(len(part) for part in frame(MEM, (OBS,), compact=True))
    assert before - after == len(f"\nsource: {OBS}")
    assert before - after >= 40, "the saving has to be worth the change at an 8,000-character budget"


def test_source_line_is_the_legacy_suffix_only() -> None:
    assert source_line((OBS,)) == f"\nsource: {OBS}"
    assert source_line(()) == ""
