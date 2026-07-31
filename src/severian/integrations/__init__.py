"""Integration adapters for external agent frameworks.

Currently provides the Hermes MemoryProvider compatibility adapter.
"""
from __future__ import annotations

from severian.integrations.hermes import SeverianMemoryProvider

__all__ = ["SeverianMemoryProvider"]
