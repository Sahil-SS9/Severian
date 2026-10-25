"""The Hermes plugin dir ships exactly the package version it pins.

Hermes PM installs a plugin's ``[project].dependencies`` into its managed
environment. The catalog wrapper (``plugins/hermes/severian/pyproject.toml``)
pins ``severian==<version>`` from PyPI, and a catalog install copies only the
plugin folder at the pinned commit, so the dependency must resolve from a
package index: no local path source and no committed archive (catalog rule 17).
If the pin, the manifest and the provider drift apart, the installed plugin
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


def test_wrapper_pins_the_package_version_from_an_index():
    version = _package()["version"]
    wrapper = _wrapper()
    assert wrapper["project"]["version"] == version
    assert wrapper["project"]["dependencies"] == [f"severian=={version}"]
    # A path/url/git source would not exist in a catalog install of the plugin folder.
    assert "sources" not in wrapper.get("tool", {}).get("uv", {})


def test_plugin_folder_holds_only_reviewable_source():
    shipped = sorted(p.relative_to(PLUGIN).as_posix() for p in PLUGIN.rglob("*") if p.is_file())
    assert shipped == ["README.md", "__init__.py", "plugin.yaml", "pyproject.toml"]


def test_wrapper_is_metadata_only():
    # No build backend: PM installs the dependencies, never builds the wrapper.
    assert "build-system" not in _wrapper()


def test_wrapper_python_floor_matches_the_package():
    assert _wrapper()["project"]["requires-python"] == _package()["requires-python"]


def test_manifest_and_provider_report_the_package_version():
    version = _package()["version"]
    assert _manifest_version() == version
    assert SeverianMemoryProvider.PROVIDER_VERSION == version
