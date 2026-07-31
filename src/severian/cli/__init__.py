"""Typer CLI for Severian.

The CLI is a thin wrapper around the canonical application service.
It accepts scope flags and a database path and routes every operation
through :class:`severian.application.service.MemoryService`. Adapters
MUST NOT bypass the service: there is no repository/index write
outside the service in this package.
"""
from __future__ import annotations

from severian.cli.app import cli

__all__ = ["cli"]
