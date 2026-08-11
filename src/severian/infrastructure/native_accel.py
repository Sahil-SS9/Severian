"""Optional native vector-search acceleration adapter (Rust plan R2.1).

Responsibilities ONLY:

- parse ``SEVERIAN_VECTOR_BACKEND=auto|python|rust`` (or constructor
  option);
- import the optional native module;
- expose the selected backend and native version;
- call exact native top-k;
- normalise native exceptions to existing Severian errors;
- fail closed for explicit ``rust`` when unavailable/broken;
- provide NO duplicate ranking implementation.

Backend identity is diagnostics/evidence only — it must never alter
vectors or ranking (plan R2.3).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from severian.domain.hits import VectorHit  # noqa: F401 (re-export for mypy)

try:  # pragma: no cover - exercised via tests
    import severian_native as _native

    _NATIVE_AVAILABLE = True
except Exception:  # noqa: BLE001 - import must never break base install
    _native = None
    _NATIVE_AVAILABLE = False

if _native is not None:
    _native_version = _native.native_version()
else:
    _native_version = None


@dataclass
class BackendDiagnostics:
    requested: str
    selected: str
    native_version: str | None = None
    fallback_reason: str | None = None
    calls: int = 0
    failures: int = 0


class NativeAccelError(RuntimeError):
    """Raised when an explicit ``rust`` backend cannot serve a call."""


class NativeAccelAdapter:
    """Selects and invokes the exact top-k backend.

    ``requested`` is one of ``auto`` (default), ``python``, ``rust``.
    Selection happens once at construction; failures during a call with
    an explicit ``rust`` request raise :class:`NativeAccelError` (fail
    closed), while ``auto`` falls back to the Python path and records
    the reason.
    """

    def __init__(self, requested: str | None = None) -> None:
        self.requested = (requested or os.environ.get("SEVERIAN_VECTOR_BACKEND", "auto")).lower()
        if self.requested not in ("auto", "python", "rust"):
            raise ValueError(
                f"SEVERIAN_VECTOR_BACKEND must be auto|python|rust, got {self.requested!r}"
            )
        self.diagnostics = BackendDiagnostics(requested=self.requested, selected="python")
        if self.requested == "python":
            self.diagnostics.selected = "python"
            self.diagnostics.fallback_reason = None
        elif _NATIVE_AVAILABLE:
            self.diagnostics.selected = "rust"
            self.diagnostics.native_version = _native_version
        else:
            self.diagnostics.selected = "python"
            self.diagnostics.fallback_reason = (
                "severian_native not installed" if self.requested == "auto"
                else "severian_native not installed (explicit rust requested)"
            )
            if self.requested == "rust":
                raise NativeAccelError(self.diagnostics.fallback_reason)

    @property
    def selected(self) -> str:
        return self.diagnostics.selected

    def exact_top_k(
        self,
        query_vector: list[float],
        rows: list[tuple[str, str, bytes]],
        k: int,
    ) -> list[VectorHit]:
        """Exact bounded top-k via the selected backend.

        ``rows``: (memory_id, content, packed_blob) — the SAME SQL
        row-fetch shape the Python path decodes.
        """
        self.diagnostics.calls += 1
        if self.diagnostics.selected == "rust":
            assert _native is not None  # selected==rust implies available
            try:
                native_rows = [(mid, content, blob) for mid, content, blob in rows]
                result = _native.exact_top_k(query_vector, native_rows, k)
                return [VectorHit(memory_id=mid, score=float(score)) for mid, score in result]
            except ValueError as exc:
                # The Rust boundary uses ValueError for malformed vector input.
                # Preserve LocalVectorIndex's public storage-contract error.
                self.diagnostics.failures += 1
                from severian.domain.errors import VectorFormatError

                raise VectorFormatError(str(exc)) from exc
            except Exception as exc:  # noqa: BLE001 - normalise + fail closed
                self.diagnostics.failures += 1
                if self.requested == "rust":
                    raise NativeAccelError(f"native top-k failed: {exc}") from exc
                self.diagnostics.selected = "python"
                self.diagnostics.fallback_reason = f"native call failed: {exc}"
                return self._python_topk(query_vector, rows, k)
        return self._python_topk(query_vector, rows, k)

    def _python_topk(
        self,
        query_vector: list[float],
        rows: list[tuple[str, str, bytes]],
        k: int,
    ) -> list[VectorHit]:
        """Pure-Python exact top-k (the reference oracle)."""
        import heapq
        import math

        from severian.infrastructure.vector import _unpack_vector

        def _key(item: tuple[VectorHit, str]) -> tuple[float, str, str]:
            hit, content = item
            return (-hit.score, content, hit.memory_id)

        scored: list[tuple[VectorHit, str]] = []
        for memory_id, content, blob in rows:
            vector = _unpack_vector(blob, len(query_vector))
            score = math.fsum(
                a * b for a, b in zip(query_vector, vector, strict=True)
            )
            scored.append((VectorHit(memory_id=memory_id, score=score), content))
        if k < len(scored):
            return [hit for hit, _content in heapq.nsmallest(k, scored, key=_key)]
        scored.sort(key=_key)
        return [hit for hit, _content in scored]
