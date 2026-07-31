"""Public API package for Severian.

The application is constructed via :func:`severian.api.app.create_app`
so tests and the runtime uvicorn entrypoint share the same composition
root. Adapters (CLI/SDK) MUST NOT bypass the ``MemoryService`` wired
here.
"""
from __future__ import annotations

from severian.api.app import create_app

__all__ = ["create_app"]
