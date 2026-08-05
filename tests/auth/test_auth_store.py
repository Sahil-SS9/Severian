"""Tests for the API-key credential store (SEC-01).

RED-first: these exercise the store's hashing, resolve, revoke, and
list behaviour. The store must never persist plaintext keys and must
fail closed on unknown / revoked keys.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from severian.auth.store import ApiKeyStore, generate_api_key, hash_key
from severian.domain.models import Scope


def _scope(**overrides: str) -> Scope:
    base = {
        "tenant_id": "t1",
        "profile_id": "p1",
        "collection_id": "c1",
        "agent_id": "a1",
        "session_id": "s1",
    }
    base.update(overrides)
    return Scope(**base)


# -- hashing ---------------------------------------------------------------

def test_hash_key_is_sha256_hex():
    digest = hash_key("secret-key")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_hash_key_is_deterministic():
    assert hash_key("secret-key") == hash_key("secret-key")


def test_hash_key_differs_for_different_input():
    assert hash_key("secret-key") != hash_key("secret-key-2")


# -- add + resolve ---------------------------------------------------------

def test_add_key_and_resolve_returns_principal(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("my-api-key", scope=_scope())
    entry = store.resolve("my-api-key")
    assert entry is not None
    assert entry.scope.tenant_id == "t1"
    assert entry.scope.profile_id == "p1"
    assert entry.revoked is False
    store.close()


def test_resolve_unknown_key_returns_none(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("known-key", scope=_scope())
    assert store.resolve("unknown-key") is None
    store.close()


def test_resolve_empty_key_returns_none(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    assert store.resolve("") is None
    assert store.resolve("   ") is None
    store.close()


def test_resolve_revoked_key_returns_none(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("revokable-key", scope=_scope(), key_id="key-1")
    assert store.revoke("revokable-key") is True
    assert store.resolve("revokable-key") is None
    store.close()


def test_revoke_unknown_key_returns_false(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    assert store.revoke("never-added") is False
    store.close()


def test_revoke_by_id_revokes_key(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("key-a", scope=_scope(), key_id="app-key-a")
    assert store.revoke_by_id("app-key-a") is True
    assert store.resolve("key-a") is None
    store.close()


def test_add_duplicate_key_raises(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("dup-key", scope=_scope())
    with pytest.raises(ValueError, match="already exists"):
        store.add_key("dup-key", scope=_scope())
    store.close()


def test_add_empty_key_raises(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    with pytest.raises(ValueError, match="non-empty"):
        store.add_key("", scope=_scope())
    store.close()


# -- different principals --------------------------------------------------

def test_keys_with_different_principals_resolve_independently(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("tenant-a-key", scope=_scope(tenant_id="a"))
    store.add_key("tenant-b-key", scope=_scope(tenant_id="b"))
    a = store.resolve("tenant-a-key")
    b = store.resolve("tenant-b-key")
    assert a is not None and b is not None
    assert a.scope.tenant_id == "a"
    assert b.scope.tenant_id == "b"
    store.close()


# -- list + metadata -------------------------------------------------------

def test_list_keys_never_returns_hash(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("listable-key", scope=_scope(), key_id="app-1")
    keys = store.list_keys()
    assert len(keys) == 1
    assert keys[0]["key_id"] == "app-1"
    # The hash must never appear in list output.
    assert "key_hash" not in keys[0]
    assert "hash" not in keys[0]
    store.close()


def test_list_keys_marks_revoked(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    store.add_key("k1", scope=_scope(), key_id="active")
    store.add_key("k2", scope=_scope(), key_id="dead")
    store.revoke("k2")
    keys = {k["key_id"]: k for k in store.list_keys()}
    assert keys["active"]["revoked"] is False
    assert keys["dead"]["revoked"] is True
    store.close()


# -- persistence -----------------------------------------------------------

def test_store_persists_across_reopen(tmp_path: Path):
    db = tmp_path / "auth.db"
    store = ApiKeyStore(db)
    store.add_key("persistent-key", scope=_scope(), key_id="persist-1")
    store.close()
    # Reopen — the key must still resolve.
    store2 = ApiKeyStore(db)
    entry = store2.resolve("persistent-key")
    assert entry is not None
    assert entry.key_id == "persist-1"
    store2.close()


# -- generated keys --------------------------------------------------------

def test_generate_api_key_is_unique_and_urlsafe():
    k1 = generate_api_key()
    k2 = generate_api_key()
    assert k1 != k2
    assert len(k1) >= 32
    # urlsafe base64 alphabet (no padding).
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
    assert set(k1) <= allowed


def test_generated_key_round_trips_through_store(tmp_path: Path):
    store = ApiKeyStore(tmp_path / "auth.db")
    key = generate_api_key()
    store.add_key(key, scope=_scope())
    assert store.resolve(key) is not None
    store.close()
