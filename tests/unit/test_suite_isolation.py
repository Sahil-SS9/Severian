"""The suite-wide guard in ``tests/conftest.py`` must drop every inherited Severian setting.

A developer shell or gateway environment exports feature switches (``SEVERIAN_DATE_STAMPS``,
``SEVERIAN_TRUTH_RESOLUTION``, ...). If one reaches a test, results depend on where the suite runs
(64 tests failed on 0.3.7 in a shell with the live gateway settings). Only the PostgreSQL test-lane
settings may pass through: CI sets them on purpose for the backend lanes.
"""
from __future__ import annotations

import os

TEST_LANE = {"SEVERIAN_PG_DSN", "SEVERIAN_PG_ALLOW_RESET"}


def test_no_inherited_severian_setting_reaches_a_test() -> None:
    leaked = sorted(name for name in os.environ if name.startswith("SEVERIAN_") and name not in TEST_LANE)
    assert leaked == []
