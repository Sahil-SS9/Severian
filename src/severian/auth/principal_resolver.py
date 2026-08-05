"""Principal resolver for the Severian HTTP API (SEC-01).

The resolver extracts a Bearer token from the ``Authorization`` header,
looks it up in the :class:`ApiKeyStore`, and returns the bound
principal :class:`Scope`. It is a callable suitable for use as a
FastAPI dependency.

Fail-closed semantics:

- When auth is **enabled** (a store is configured), a request with no
  ``Authorization: Bearer`` header, an invalid scheme, or an unknown
  key raises :class:`AuthDisabledError`-equivalent → 401.
- When auth is **disabled** (no store), the resolver returns ``None``
  and the request proceeds in dev/loopback mode (client supplies
  scope, as before).

This keeps the existing ``principal: Scope | None`` contract in
``create_app`` intact: the static ``principal`` path is preserved for
tests and construct-time binding (MCP/Hermes), while the resolver
adds per-request authentication.
"""
from __future__ import annotations

from fastapi import HTTPException, Request, status

from severian.auth.store import ApiKeyStore
from severian.domain.models import Scope

_BEARER_PREFIX = "Bearer "


class AuthDisabledError(RuntimeError):
    """Raised when auth is required but not configured."""


class PrincipalResolver:
    """Callable that resolves a Bearer token to a principal Scope.

    Construct with an :class:`ApiKeyStore` to enable authentication.
    Construct with ``store=None`` to disable authentication (dev/loopback
    mode — returns ``None`` so the request uses client-supplied scope).
    """

    def __init__(self, store: ApiKeyStore | None = None) -> None:
        self._store = store

    @property
    def enabled(self) -> bool:
        return self._store is not None

    def __call__(self, request: Request) -> Scope | None:
        """FastAPI dependency: resolve the principal from the request.

        When the store is configured, a missing/invalid Bearer token
        raises 401. When the store is not configured, returns ``None``
        (dev mode).
        """
        if self._store is None:
            return None
        auth_header = request.headers.get("authorization", "")
        token = self._extract_bearer(auth_header)
        if token is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"detail": "missing or invalid Authorization header", "type": "auth_error"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        entry = self._store.resolve(token)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"detail": "invalid or revoked api key", "type": "auth_error"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        return entry.scope

    @staticmethod
    def _extract_bearer(header: str) -> str | None:
        """Extract the token from an ``Authorization: Bearer <token>`` header.

        Returns ``None`` if the header is missing or not a Bearer scheme.
        The comparison is case-insensitive on the scheme name per RFC 6750.
        """
        if not header:
            return None
        if not header.lower().startswith(_BEARER_PREFIX.lower()):
            return None
        token = header[len(_BEARER_PREFIX):].strip()
        if not token:
            return None
        return token


def resolve_principal_from_request(
    request: Request,
    store: ApiKeyStore | None,
) -> Scope | None:
    """Standalone resolver (for non-FastAPI contexts or testing).

    Mirrors :class:`PrincipalResolver.__call__` but returns the
    resolved scope (or ``None`` when disabled) without raising on
    auth failure — callers must check for ``None`` and reject.
    """
    if store is None:
        return None
    resolver = PrincipalResolver(store)
    return resolver(request)


__all__ = [
    "AuthDisabledError",
    "PrincipalResolver",
    "resolve_principal_from_request",
]
