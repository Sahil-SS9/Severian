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


class VectorFormatError(RuntimeError):
    """The vector projection uses an unsupported/legacy storage format.

    Raised when a legacy JSON-vector index is opened for writes: the
    projection is disposable — rebuild from canonical memories instead
    of migrating rows in place (zero-debt 4.1).
    """

    pass


class ConflictError(RuntimeError):
    pass


class ForbiddenError(PermissionError):
    """Authorization failure: the principal may not access the scope."""


__all__ = ["ConflictError", "ForbiddenError", "NotFoundError", "VectorFormatError"]
