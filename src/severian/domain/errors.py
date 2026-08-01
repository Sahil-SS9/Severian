"""Shared domain errors for the canonical memory platform.

These exceptions are backend-neutral: both ``SQLiteRepository`` and
``PostgreSQLBackend`` raise the same classes so that application code
and adapters can catch one error type regardless of the active backend.
They intentionally do NOT subclass each other's distinct builtins in a
way that leaks implementation detail:

- ``NotFoundError`` — the resource does not exist in the requested
  scope (or the caller may not know it exists in another scope). The
  canonical store must never reveal existence in a foreign scope; this
  is the only signal a caller receives.
- ``ConflictError`` — an identity, immutability, or state transition
  conflict (duplicate scoped identity, revision of an already
  superseded head, completion of an unclaimed job, import conflict).
"""


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


__all__ = ["ConflictError", "NotFoundError"]
