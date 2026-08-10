"""severian-native: optional native vector-search acceleration.

Public surface (diagnostics contract R2.3):

- ``exact_top_k(query, rows, k)`` — exact bounded top-k, fsum parity;
- ``native_version()`` — module/version string for diagnostics;
- ``available`` — True when the compiled extension imported.

The base ``severian`` package works WITHOUT this package (pure-Python
fallback); this is an optional speedup companion only.
"""
from __future__ import annotations

from ._severian_native import exact_top_k, native_version

__all__ = ["available", "exact_top_k", "native_version"]

available = True
