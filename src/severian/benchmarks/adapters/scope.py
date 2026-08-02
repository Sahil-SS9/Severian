"""Scope parsing shared by benchmark adapters.

Kept in its own module to avoid the circular import between
``adapters/__init__`` and the concrete adapters.
"""
from __future__ import annotations


def parse_scope(value: str) -> tuple[str, str, str, str, str]:
    """Parse the manifest's ``t/p/c/a/s`` scope string into five parts.

    Raises ``ValueError`` on any malformed scope — the benchmark must
    never guess a scope boundary.
    """
    parts = value.strip().split("/")
    if len(parts) != 5 or any(not part for part in parts):
        raise ValueError(f"invalid scope: {value!r} (expected t/p/c/a/s)")
    return tuple(parts)  # type: ignore[return-value]
