"""Severian Hermes memory plugin.

P1-4: this directory is a drop-in Hermes memory provider plugin. Install
it as ``$HERMES_HOME/plugins/severian/`` (or bundle it under
``plugins/memory/severian/`` in a Hermes checkout) and set
``memory.provider: severian`` in config.yaml. The Hermes loader calls
``register(ctx)`` which hands the provider to the loader's
``register_memory_provider`` context hook.

The plugin is a thin shim: all behaviour lives in
:mod:`severian.integrations.hermes`. Hermes PM installs the ``severian``
package from the pinned wheel this directory's ``pyproject.toml`` declares
(built by ``scripts/build_hermes_plugin.py``).
"""
from __future__ import annotations

from typing import Any

from severian.integrations.hermes import register as _register

__all__ = ["register"]


def register(ctx: Any) -> None:
    """Hermes loader entry point — delegates to the Severian adapter."""
    _register(ctx)
