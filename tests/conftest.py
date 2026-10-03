"""Suite-wide guard: no test may reach a real Hermes home or Severian store.

The Hermes adapter falls back to ``<HERMES_HOME root>/severian`` when
``SEVERIAN_STORAGE`` is unset. Pin ``HERMES_HOME`` to a per-test scratch
directory so that fallback can never resolve to the live ``~/.hermes`` store,
and drop inherited Severian deployment variables so a developer shell or a
gateway environment cannot leak into test results.
"""
from __future__ import annotations

import pytest

_DEPLOYMENT_ENV = (
    "SEVERIAN_STORAGE",
    "SEVERIAN_DSN",
    "SEVERIAN_EMBEDDING",
    "SEVERIAN_EMBED_SOCKET",
    "SEVERIAN_TENANT",
)


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path_factory.mktemp("hermes-home")))
    for name in _DEPLOYMENT_ENV:
        monkeypatch.delenv(name, raising=False)
