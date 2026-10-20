"""Suite-wide guard: no test may reach a real Hermes home or Severian store.

The Hermes adapter falls back to ``<HERMES_HOME root>/severian`` when
``SEVERIAN_STORAGE`` is unset. Pin ``HERMES_HOME`` to a per-test scratch
directory so that fallback can never resolve to the live ``~/.hermes`` store,
and drop EVERY inherited Severian variable (deployment settings and feature
switches alike) so a developer shell or a gateway environment cannot leak into
test results. Only the PostgreSQL test-lane settings pass through: CI sets them
on purpose for the backend lanes. A test that needs a setting sets it itself.
"""
from __future__ import annotations

import os

import pytest

_TEST_LANE_ENV = frozenset({"SEVERIAN_PG_DSN", "SEVERIAN_PG_ALLOW_RESET"})


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
    for name in [name for name in os.environ if name.startswith("SEVERIAN_") and name not in _TEST_LANE_ENV]:
        monkeypatch.delenv(name, raising=False)
