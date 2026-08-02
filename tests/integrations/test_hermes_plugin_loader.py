"""P1-4: prove the Severian Hermes plugin loads through the REAL Hermes loader.

These tests exercise ``plugins.memory.load_memory_provider`` from a real
Hermes checkout (KenseiAgent). They are skipped when the checkout is not
present on this machine.
"""
from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

KENSEI_AGENT = Path("/home/kensei/repos/KenseiAgent")
SEVERIAN_PLUGIN = Path("/home/kensei/repos/Severian/plugins/hermes/severian")

pytestmark = pytest.mark.skipif(
    not KENSEI_AGENT.is_dir() or not SEVERIAN_PLUGIN.is_dir(),
    reason="requires KenseiAgent Hermes checkout and Severian plugin",
)


@pytest.fixture
def hermes_plugins():
    """Load the real Hermes memory-plugin loader in isolation."""
    importlib.invalidate_caches()
    sys.path.insert(0, str(KENSEI_AGENT))
    yield importlib.import_module("plugins.memory")
    sys.path.remove(str(KENSEI_AGENT))
    for name in list(sys.modules):
        if name.startswith("plugins.memory") or name == "plugins":
            sys.modules.pop(name, None)
    importlib.invalidate_caches()


def test_real_hermes_loader_activates_severian(tmp_path, monkeypatch, hermes_plugins):
    # P1-4 repro: the provider required an injected service, so the real
    # loader (register(ctx) / zero-arg ctor) could never activate it.
    # Install the shipped plugin into a scratch HERMES_HOME and load it.
    home = tmp_path / "hermes-home"
    plugin_dir = home / "plugins" / "severian"
    plugin_dir.mkdir(parents=True)
    shutil.copy2(SEVERIAN_PLUGIN / "__init__.py", plugin_dir / "__init__.py")
    shutil.copy2(SEVERIAN_PLUGIN / "plugin.yaml", plugin_dir / "plugin.yaml")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("SEVERIAN_STORAGE", str(tmp_path / "storage"))
    monkeypatch.delenv("SEVERIAN_DSN", raising=False)

    # The real loader resolves user plugins via hermes_constants which
    # reads HERMES_HOME at call time.
    loader = hermes_plugins
    provider = loader.load_memory_provider("severian")
    assert provider is not None, "real Hermes loader returned None for severian"
    assert provider.name == "severian"
    try:
        assert provider.is_available() is True
    finally:
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()


def test_real_hermes_loader_discover_includes_severian(tmp_path, monkeypatch, hermes_plugins):
    home = tmp_path / "hermes-home"
    plugin_dir = home / "plugins" / "severian"
    plugin_dir.mkdir(parents=True)
    shutil.copy2(SEVERIAN_PLUGIN / "__init__.py", plugin_dir / "__init__.py")
    shutil.copy2(SEVERIAN_PLUGIN / "plugin.yaml", plugin_dir / "plugin.yaml")

    monkeypatch.setenv("HERMES_HOME", str(home))
    names = [name for name, _desc, _avail in hermes_plugins.discover_memory_providers()]
    assert "severian" in names
