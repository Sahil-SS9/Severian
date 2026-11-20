"""M3 launch-contract tests for ``python -m severian.mcp``.

The stdio transport cannot perform interactive authentication, so launch
must pin tenant/profile or explicitly opt into trusted single-tenant mode.
These tests exercise the parser/resolver without opening a store.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from severian.mcp.server import MCPPrincipal

mcp_main = importlib.import_module("severian.mcp.__main__")


class _FakeServer:
    def __init__(self) -> None:
        self.ran = False

    def run_stdio(self) -> None:
        self.ran = True


class _Closer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _paths() -> list[str]:
    return ["--database", "s.db", "--fts", "f.db", "--vectors", "v.db"]


def _install_fake_build(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def fake_build(**kwargs: Any):
        captured.update(kwargs)
        captured["server"] = _FakeServer()
        captured["closers"] = [_Closer(), _Closer(), _Closer()]
        return captured["server"], *captured["closers"]

    monkeypatch.setattr(mcp_main, "_build_server", fake_build)
    return captured


def _clear_principal_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(mcp_main.TENANT_ENV, raising=False)
    monkeypatch.delenv(mcp_main.PROFILE_ENV, raising=False)


# -- resolver -------------------------------------------------------------


def test_resolver_flags_pin_principal(monkeypatch: pytest.MonkeyPatch):
    _clear_principal_env(monkeypatch)
    principal = mcp_main.resolve_principal("acme", "work", trusted_single_tenant=False)
    assert principal == MCPPrincipal(tenant_id="acme", profile_id="work")


def test_resolver_environment_pins_principal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(mcp_main.TENANT_ENV, "acme")
    monkeypatch.setenv(mcp_main.PROFILE_ENV, "work")
    principal = mcp_main.resolve_principal(None, None, trusted_single_tenant=False)
    assert principal == MCPPrincipal(tenant_id="acme", profile_id="work")


def test_resolver_flags_override_environment(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(mcp_main.TENANT_ENV, "evil")
    monkeypatch.setenv(mcp_main.PROFILE_ENV, "other")
    principal = mcp_main.resolve_principal("acme", "work", trusted_single_tenant=False)
    assert principal == MCPPrincipal(tenant_id="acme", profile_id="work")


@pytest.mark.parametrize("trusted", [False, True])
def test_resolver_refuses_partial_principal(monkeypatch: pytest.MonkeyPatch, trusted: bool):
    _clear_principal_env(monkeypatch)
    with pytest.raises(ValueError, match="incomplete"):
        mcp_main.resolve_principal("acme", None, trusted_single_tenant=trusted)


def test_resolver_refuses_blank_principal(monkeypatch: pytest.MonkeyPatch):
    _clear_principal_env(monkeypatch)
    with pytest.raises(ValueError, match="incomplete"):
        mcp_main.resolve_principal("acme", "   ", trusted_single_tenant=False)


def test_resolver_trusted_opt_out_allows_no_principal(monkeypatch: pytest.MonkeyPatch):
    _clear_principal_env(monkeypatch)
    assert mcp_main.resolve_principal(None, None, trusted_single_tenant=True) is None


def test_resolver_principal_wins_over_trusted_opt_out(monkeypatch: pytest.MonkeyPatch):
    _clear_principal_env(monkeypatch)
    principal = mcp_main.resolve_principal("acme", "work", trusted_single_tenant=True)
    assert principal == MCPPrincipal(tenant_id="acme", profile_id="work")


def test_principal_rejects_blank_dimensions():
    with pytest.raises(ValidationError):
        MCPPrincipal(tenant_id=" ", profile_id="work")


# -- CLI launch -----------------------------------------------------------


def test_bare_launch_fails_closed_and_names_flags(monkeypatch: pytest.MonkeyPatch, capsys):
    _clear_principal_env(monkeypatch)
    called = _install_fake_build(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_main.main(_paths())
    assert exc.value.code == 2
    err = capsys.readouterr().err
    for flag in (
        "--tenant-id",
        "--profile-id",
        "--trusted-single-tenant",
        mcp_main.TENANT_ENV,
        mcp_main.PROFILE_ENV,
    ):
        assert flag in err
    assert "database" not in called  # build never runs before the refusal


def test_partial_flag_fails_closed(monkeypatch: pytest.MonkeyPatch, capsys):
    _clear_principal_env(monkeypatch)
    _install_fake_build(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_main.main([*_paths(), "--tenant-id", "acme"])
    assert exc.value.code == 2
    assert "--profile-id" in capsys.readouterr().err


def test_flags_bind_principal_and_run(monkeypatch: pytest.MonkeyPatch):
    _clear_principal_env(monkeypatch)
    captured = _install_fake_build(monkeypatch)
    assert mcp_main.main([*_paths(), "--tenant-id", "acme", "--profile-id", "work"]) == 0
    assert captured["principal"] == MCPPrincipal(tenant_id="acme", profile_id="work")
    assert captured["server"].ran
    assert all(closer.closed for closer in captured["closers"])


def test_environment_binds_principal_and_runs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(mcp_main.TENANT_ENV, "acme")
    monkeypatch.setenv(mcp_main.PROFILE_ENV, "work")
    captured = _install_fake_build(monkeypatch)
    assert mcp_main.main(_paths()) == 0
    assert captured["principal"] == MCPPrincipal(tenant_id="acme", profile_id="work")


def test_trusted_opt_out_runs_unbound_with_warning(monkeypatch: pytest.MonkeyPatch, capsys):
    _clear_principal_env(monkeypatch)
    captured = _install_fake_build(monkeypatch)
    assert mcp_main.main([*_paths(), "--trusted-single-tenant"]) == 0
    assert captured["principal"] is None
    assert "trusted-context" in capsys.readouterr().err.lower()


def test_principal_and_opt_out_still_binds(monkeypatch: pytest.MonkeyPatch):
    _clear_principal_env(monkeypatch)
    captured = _install_fake_build(monkeypatch)
    assert mcp_main.main([*_paths(), "--tenant-id", "acme", "--profile-id", "work", "--trusted-single-tenant"]) == 0
    assert captured["principal"] == MCPPrincipal(tenant_id="acme", profile_id="work")


def test_database_paths_still_required(monkeypatch: pytest.MonkeyPatch):
    _clear_principal_env(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        mcp_main.main(["--tenant-id", "acme", "--profile-id", "work"])
    assert exc.value.code == 2


def test_path_arguments_are_passed_through(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    captured = _install_fake_build(monkeypatch)
    db, fts, vectors = tmp_path / "s.db", tmp_path / "f.db", tmp_path / "v.db"
    assert (
        mcp_main.main(
            [
                "--database",
                str(db),
                "--fts",
                str(fts),
                "--vectors",
                str(vectors),
                "--tenant-id",
                "acme",
                "--profile-id",
                "work",
            ]
        )
        == 0
    )
    assert captured["database"] == db
    assert captured["fts"] == fts
    assert captured["vectors"] == vectors
