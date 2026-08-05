"""Authentication and principal-bound scope for Severian.

SEC-01: real API-key authentication with a fail-closed HTTP API.
Keys are hashed at rest (SHA-256); each key carries a scoped
principal :class:`Scope` that becomes the authorization context for
the request. The HTTP API rejects unauthenticated requests (401)
and cross-tenant claims (403 via the existing ``_bind_principal``).
"""

from severian.auth.principal_resolver import (
    AuthDisabledError,
    PrincipalResolver,
    resolve_principal_from_request,
)
from severian.auth.store import ApiKeyStore, KeyEntry, hash_key

__all__ = [
    "ApiKeyStore",
    "AuthDisabledError",
    "KeyEntry",
    "PrincipalResolver",
    "hash_key",
    "resolve_principal_from_request",
]
