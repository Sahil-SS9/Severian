"""Catalog review (hermes-agent PR 142812): credentials through Hermes, no process-wide env changes.

1. SEVERIAN_DSN can carry a database password, so it is read with Hermes's profile-aware
   ``agent.secret_scope.get_secret`` (catalog rule 11), falling back to the environment only
   on a Hermes that has no secret scope.
2. The opt-in embedding loaders pass offline mode to the loader instead of setting
   HF_HUB_OFFLINE / TQDM_DISABLE for the whole Hermes process.
3. plugin.yaml declares SEVERIAN_DSN under requires_env, as an optional secret.
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
import yaml

from severian.integrations import hermes

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fake_secret_scope(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    scope: dict[str, str] = {}

    class UnscopedSecretError(RuntimeError):
        pass

    mod = types.ModuleType("agent.secret_scope")

    def get_secret(name: str, default: str | None = None) -> str | None:
        if scope.get("__raise__"):
            raise UnscopedSecretError(name)
        return scope.get(name, default)

    mod.get_secret = get_secret  # type: ignore[attr-defined]
    mod.UnscopedSecretError = UnscopedSecretError  # type: ignore[attr-defined]
    pkg = types.ModuleType("agent")
    pkg.secret_scope = mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", mod)
    return scope


def test_dsn_comes_from_the_profile_secret_scope_not_the_process_env(
    fake_secret_scope: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEVERIAN_DSN", "postgresql://other-profile@db/x")
    fake_secret_scope["SEVERIAN_DSN"] = "postgresql://this-profile@db/x"
    assert hermes._severian_dsn() == "postgresql://this-profile@db/x"


def test_dsn_unset_in_scope_means_sqlite_even_if_env_has_one(
    fake_secret_scope: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under multiplexing get_secret returns the default for a miss; the env value must not leak in.
    monkeypatch.setenv("SEVERIAN_DSN", "postgresql://other-profile@db/x")
    assert hermes._severian_dsn() == ""


def test_unscoped_read_fails_closed(fake_secret_scope: dict[str, str]) -> None:
    fake_secret_scope["__raise__"] = "1"
    with pytest.raises(RuntimeError):
        hermes._severian_dsn()


def test_older_hermes_without_secret_scope_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)  # import raises ImportError
    monkeypatch.setenv("SEVERIAN_DSN", " postgresql://me@db/x ")
    assert hermes._severian_dsn() == "postgresql://me@db/x"


def test_only_the_pre_secret_scope_fallback_reads_the_dsn_from_os_environ() -> None:
    import inspect

    src = inspect.getsource(hermes)
    assert src.count('os.environ.get("SEVERIAN_DSN"') == 1
    assert 'os.environ.get("SEVERIAN_DSN"' in inspect.getsource(hermes._severian_dsn)


def _fake_sentence_transformers(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    class SentenceTransformer:
        def __init__(self, name: str, **kwargs: object) -> None:
            calls.append({"name": name, **kwargs})

    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = SentenceTransformer  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    return calls


@pytest.mark.parametrize(
    ("module", "cls", "cache"),
    [
        ("severian.infrastructure.qwen_embedding", "QwenEmbedding", "models--Qwen--Qwen3-Embedding-0.6B"),
        (
            "severian.infrastructure.granite_embedding",
            "GraniteR2Embedding",
            "models--ibm-granite--granite-embedding-107m-multilingual",
        ),
    ],
)
@pytest.mark.parametrize("cached", [True, False])
def test_embedding_loaders_leave_the_process_env_alone(
    module: str, cls: str, cache: str, cached: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TQDM_DISABLE", raising=False)
    if cached:
        (tmp_path / ".cache/huggingface/hub" / cache).mkdir(parents=True)
    calls = _fake_sentence_transformers(monkeypatch)
    before = dict(os.environ)

    embedder = getattr(importlib.import_module(module), cls)()
    assert embedder.load() is True

    assert dict(os.environ) == before
    assert calls and calls[0].get("local_files_only", False) is cached


def test_plugin_manifest_declares_the_dsn_as_an_optional_secret() -> None:
    manifest = yaml.safe_load((ROOT / "plugins/hermes/severian/plugin.yaml").read_text())
    specs = {e["name"] if isinstance(e, dict) else e: e for e in manifest.get("requires_env") or []}
    assert "SEVERIAN_DSN" in specs
    spec = specs["SEVERIAN_DSN"]
    assert isinstance(spec, dict) and spec.get("secret") is True
    assert "optional" in spec.get("description", "").lower()
