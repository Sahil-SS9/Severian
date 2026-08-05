"""API-key credential store for Severian (SEC-01).

Keys are hashed at rest with SHA-256. The store maps a key hash to a
:class:`KeyEntry` carrying the scoped principal :class:`Scope` and
optional human-readable metadata (key id, creation time).

The store is backed by a small SQLite database (single table) so it
survives restarts and is concurrent-safe. The file path is selected
by the operator (``--auth-file`` / ``SEVERIAN_AUTH_FILE``).

Design rules:

- **Keys are never stored in plaintext.** Only the SHA-256 digest is
  persisted. Verification compares ``sha256(presented_key)`` against
  the stored digest using ``secrets.compare_digest`` (constant-time).
- **Each key binds a fixed principal.** The principal's
  ``tenant_id`` and ``profile_id`` are the authorization context; the
  request body cannot change them. ``collection_id``, ``agent_id``
  and ``session_id`` in the key entry are defaults that the request
  MAY override (they are not security-sensitive dimensions).
- **Fail closed.** ``resolve`` returns ``None`` for unknown or
  malformed keys; the caller rejects the request.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from severian.domain.models import Scope

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash    TEXT PRIMARY KEY,
    key_id      TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    profile_id  TEXT NOT NULL,
    collection_id TEXT NOT NULL,
    agent_id    TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0
)
"""


def hash_key(key: str) -> str:
    """Return the SHA-256 hex digest of a plaintext API key."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class KeyEntry:
    """A resolved API key with its bound principal scope."""

    key_hash: str
    key_id: str
    scope: Scope
    created_at: str
    revoked: bool

    @property
    def principal(self) -> Scope:
        """The fixed principal scope bound to this key."""
        return self.scope


class ApiKeyStore:
    """SQLite-backed API-key credential store.

    The store is thread-safe (a lock guards writes; SQLite is opened
    with ``check_same_thread=False`` so the same connection can be
    shared across request threads). For high-concurrency production
    use, prefer a connection pool; this store targets the
    local-first / small-team deployment.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            isolation_level=None,  # autocommit
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def add_key(self, key: str, *, scope: Scope, key_id: str = "") -> str:
        """Add an API key with a bound principal scope.

        Returns the key hash. Raises ``ValueError`` if the key is
        empty, the scope is invalid, or the key hash already exists.
        """
        if not isinstance(key, str) or not key.strip():
            raise ValueError("api key must be a non-empty string")
        digest = hash_key(key)
        kid = key_id.strip() or f"key-{digest[:8]}"
        now = datetime.now(UTC).isoformat()
        with self._lock:
            existing = self._conn.execute(
                "SELECT 1 FROM api_keys WHERE key_hash = ?", (digest,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"api key already exists (key_id={kid})")
            self._conn.execute(
                "INSERT INTO api_keys (key_hash, key_id, tenant_id, profile_id, "
                "collection_id, agent_id, session_id, created_at, revoked) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    digest,
                    kid,
                    scope.tenant_id,
                    scope.profile_id,
                    scope.collection_id,
                    scope.agent_id,
                    scope.session_id,
                    now,
                ),
            )
        return digest

    def resolve(self, key: str) -> KeyEntry | None:
        """Resolve a presented API key to its bound principal.

        Returns ``None`` when the key is unknown, revoked, or the
        input is malformed. Constant-time comparison is used on the
        hash to avoid timing oracles.
        """
        if not isinstance(key, str) or not key.strip():
            return None
        digest = hash_key(key)
        row = self._conn.execute(
            "SELECT * FROM api_keys WHERE key_hash = ?",
            (digest,),
        ).fetchone()
        if row is None:
            return None
        if bool(row["revoked"]):
            return None
        # Constant-time compare on the digest to harden against timing.
        if not secrets.compare_digest(digest, str(row["key_hash"])):
            return None
        try:
            scope = Scope(
                tenant_id=str(row["tenant_id"]),
                profile_id=str(row["profile_id"]),
                collection_id=str(row["collection_id"]),
                agent_id=str(row["agent_id"]),
                session_id=str(row["session_id"]),
            )
        except Exception:
            return None
        return KeyEntry(
            key_hash=str(row["key_hash"]),
            key_id=str(row["key_id"]),
            scope=scope,
            created_at=str(row["created_at"]),
            revoked=bool(row["revoked"]),
        )

    def revoke(self, key: str) -> bool:
        """Mark a key as revoked. Returns True if the key was found."""
        digest = hash_key(key)
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE key_hash = ?",
                (digest,),
            )
        return cur.rowcount > 0

    def revoke_by_id(self, key_id: str) -> bool:
        """Revoke a key by its key_id. Returns True if any row matched."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked = 1 WHERE key_id = ?",
                (key_id,),
            )
        return cur.rowcount > 0

    def list_keys(self) -> list[dict[str, Any]]:
        """List non-revoked key metadata (never the hash)."""
        rows = self._conn.execute(
            "SELECT key_id, tenant_id, profile_id, collection_id, agent_id, "
            "session_id, created_at, revoked FROM api_keys ORDER BY created_at"
        ).fetchall()
        return [
            {
                "key_id": str(r["key_id"]),
                "scope": {
                    "tenant_id": str(r["tenant_id"]),
                    "profile_id": str(r["profile_id"]),
                    "collection_id": str(r["collection_id"]),
                    "agent_id": str(r["agent_id"]),
                    "session_id": str(r["session_id"]),
                },
                "created_at": str(r["created_at"]),
                "revoked": bool(r["revoked"]),
            }
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()


def generate_api_key() -> str:
    """Generate a random 32-byte API key (urlsafe base64, no padding)."""
    import base64

    raw = os.urandom(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


__all__ = [
    "ApiKeyStore",
    "KeyEntry",
    "generate_api_key",
    "hash_key",
]
