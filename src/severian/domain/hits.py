"""Projection hit value types (ARCH-03).

``FTSHit`` and ``VectorHit`` are pure value objects returned by the
lexical/vector projections. They contain no storage or adapter logic, so
they live in the domain and infrastructure re-exports them for backward
compatibility — application code never needs to import infrastructure to
name a hit type.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FTSHit:
    memory_id: str
    score: float
    snippet: str


@dataclass(frozen=True)
class VectorHit:
    memory_id: str
    score: float
