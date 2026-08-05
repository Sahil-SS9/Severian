"""Tests for the non-loopback startup guard in serve_dev.py (SEC-01).

RED-first: ``serve_dev`` must refuse to start on a non-loopback host
unless ``--auth-file`` is provided. The guard functions are tested
directly so we don't need to spin up a real server.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.serve_dev import (  # noqa: E402
    NonLoopbackGuardError,  # noqa: E402
    enforce_loopback_guard,
    is_loopback_host,
    should_require_auth,
)


def test_localhost_is_loopback():
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("::1") is True


def test_non_loopback_is_not_loopback():
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("192.168.1.5") is False
    assert is_loopback_host("example.com") is False


def test_loopback_does_not_require_auth():
    assert should_require_auth(host="127.0.0.1", auth_file=None) is False
    assert should_require_auth(host="localhost", auth_file=None) is False


def test_non_loopback_without_auth_requires_auth():
    assert should_require_auth(host="0.0.0.0", auth_file=None) is True
    assert should_require_auth(host="192.168.1.5", auth_file=None) is True


def test_non_loopback_with_auth_file_ok():
    assert should_require_auth(host="0.0.0.0", auth_file="/tmp/auth.db") is False


def test_guard_raises_on_non_loopback_without_auth():
    with pytest.raises(NonLoopbackGuardError, match="auth"):
        enforce_loopback_guard(host="0.0.0.0", auth_file=None)


def test_guard_passes_on_loopback():
    enforce_loopback_guard(host="127.0.0.1", auth_file=None)


def test_guard_passes_on_non_loopback_with_auth():
    enforce_loopback_guard(host="0.0.0.0", auth_file="/tmp/auth.db")
