"""The Hermes plugin dir ships exactly the package version it pins.

Hermes PM installs a plugin's ``[project].dependencies`` into its managed
environment. Severian is not on a package index, so the plugin's catalog
wrapper (``plugins/hermes/severian/pyproject.toml``) pins the wheel that
``scripts/build_hermes_plugin.py`` places in ``wheels/``. If the pin, the
wheel path, the manifest and the provider drift apart, the deployed plugin
either fails to resolve or reports a version it is not running.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from severian.integrations.hermes import SeverianMemoryProvider

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "hermes" / "severian"


def _package() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _wrapper() -> dict:
    return tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))


def _manifest_version() -> str:
    for line in (PLUGIN / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip().strip("\"'")
    raise AssertionError("plugin.yaml declares no version")


def test_wrapper_pins_the_wheel_it_ships():
    version = _package()["version"]
    wrapper = _wrapper()
    assert wrapper["project"]["version"] == version
    assert wrapper["project"]["dependencies"] == [f"severian=={version}"]
    assert wrapper["tool"]["uv"]["sources"]["severian"] == {"path": f"wheels/severian-{version}-py3-none-any.whl"}


def test_wrapper_is_metadata_only():
    # No build backend: PM installs the dependencies, never builds the wrapper.
    assert "build-system" not in _wrapper()


def test_wrapper_python_floor_matches_the_package():
    assert _wrapper()["project"]["requires-python"] == _package()["requires-python"]


def test_manifest_and_provider_report_the_package_version():
    version = _package()["version"]
    assert _manifest_version() == version
    assert SeverianMemoryProvider.PROVIDER_VERSION == version
