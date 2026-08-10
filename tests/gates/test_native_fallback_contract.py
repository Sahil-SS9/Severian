"""R1.1/R2.3 fallback contract: base Severian works without native code.

Proves:
- importing `severian` succeeds when `severian_native` is absent;
- `backend=python` always uses the Python path;
- `backend=rust` raises a clear configuration error when native is
  absent;
- `backend=auto` selects Python when native is absent and reports
  `python` through diagnostics;
- no import exception is silently swallowed without backend state
  being observable.

The native module presence is forced to "absent" by simulating the
import failure path of native_accel (monkeypatched import).
"""
from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from severian.domain.models import Memory, Scope
from severian.infrastructure.vector import LocalVectorIndex

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


@pytest.fixture()
def native_absent(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force severian_native import to fail, then reload native_accel.

    Restores module state afterwards: severian_native is re-imported and
    native_accel reloaded so later tests in the session see the real
    availability (without this, a reloaded native_accel stays poisoned
    with _NATIVE_AVAILABLE=False and breaks integration tests).
    """
    import builtins

    real_import = builtins.__import__
    had_native = "severian_native" in sys.modules
    had_accel = "severian.infrastructure.native_accel" in sys.modules

    def fake_import(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] | None = None,
        level: int = 0,
    ) -> object:
        if name == "severian_native" or name.startswith("severian_native."):
            raise ModuleNotFoundError("severian_native not installed (test)")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    for mod in list(sys.modules):
        if mod == "severian_native" or mod.startswith("severian_native."):
            del sys.modules[mod]
    if "severian.infrastructure.native_accel" in sys.modules:
        del sys.modules["severian.infrastructure.native_accel"]
    import severian.infrastructure.native_accel as na

    importlib.reload(na)
    assert na._NATIVE_AVAILABLE is False
    yield
    # Teardown: restore real module state for later tests in the session.
    monkeypatch.undo()
    if had_native and "severian_native" not in sys.modules:
        import severian_native  # type: ignore[import-untyped]  # noqa: F401
    if had_accel and "severian.infrastructure.native_accel" in sys.modules:
        del sys.modules["severian.infrastructure.native_accel"]
    import severian.infrastructure.native_accel as na2

    importlib.reload(na2)


def test_import_severian_succeeds_without_native(native_absent: None) -> None:
    import severian  # noqa: F401
    from severian.infrastructure.vector import LocalVectorIndex  # noqa: F401


def test_backend_python_forced(native_absent: None) -> None:
    from severian.infrastructure.native_accel import NativeAccelAdapter

    adapter = NativeAccelAdapter("python")
    assert adapter.selected == "python"
    assert adapter.diagnostics.fallback_reason is None
    # call path executes Python top-k
    query = [1.0, 0.0, 0.0]
    rows = [("m1", "c1", __import__("struct").pack("<3f", 1.0, 0.0, 0.0))]
    hits = adapter.exact_top_k(query, rows, 1)
    assert [(h.memory_id, h.score) for h in hits] == [("m1", 1.0)]


def test_backend_rust_fails_closed_when_absent(native_absent: None) -> None:
    from severian.infrastructure.native_accel import NativeAccelAdapter

    with pytest.raises(RuntimeError, match="severian_native not installed"):
        NativeAccelAdapter("rust")


def test_backend_auto_selects_python_and_reports(native_absent: None) -> None:
    from severian.infrastructure.native_accel import NativeAccelAdapter

    adapter = NativeAccelAdapter("auto")
    assert adapter.selected == "python"
    assert adapter.diagnostics.requested == "auto"
    assert adapter.diagnostics.fallback_reason == "severian_native not installed"


def test_invalid_backend_value_rejected(native_absent: None) -> None:
    from severian.infrastructure.native_accel import NativeAccelAdapter

    with pytest.raises(ValueError, match="auto\\|python\\|rust"):
        NativeAccelAdapter("cuda")


def test_fallback_recorded_not_silent(native_absent: None) -> None:
    """auto mode falling back must leave observable state, not silence."""
    from severian.infrastructure.native_accel import NativeAccelAdapter

    adapter = NativeAccelAdapter("auto")
    assert adapter.diagnostics.selected == "python"
    assert adapter.diagnostics.fallback_reason is not None
    # diagnostics visible through the index
    from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

    index = LocalVectorIndex(Path("/tmp/test-native-fallback.db"), HashEmbedding(dimension=8))
    diag = index.backend_diagnostics()
    assert diag["requested"] == "auto"
    assert diag["selected"] in ("python", "rust")
    index.close()


# --- P0-A: explicit Rust must fail closed through the PUBLIC search() -------

def _scope() -> Scope:
    return Scope(
        tenant_id="t", profile_id="p", collection_id="c",
        agent_id="a", session_id="s",
    )


def _insert_one(index: LocalVectorIndex) -> None:
    index.upsert(Memory(
        id="m1", scope=_scope(), kind="derived",
        content="the red fox runs fast through the forest",
        source_observation_ids=["obs-1"], model_id="hash-8",
        prompt_version="v1", confidence=0.9,
    ))


def test_explicit_rust_through_public_search_fails_closed(
    native_absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RED (P0-A): with SEVERIAN_VECTOR_BACKEND=rust and the native module
    absent, LocalVectorIndex.search() must raise the documented typed error —
    it must NEVER silently return Python hits. The audited code caught every
    adapter-construction exception and built NativeAccelAdapter('python')."""
    from severian.infrastructure.native_accel import NativeAccelError
    from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

    monkeypatch.setenv("SEVERIAN_VECTOR_BACKEND", "rust")
    index = LocalVectorIndex(tmp_path / "fail-closed.db", HashEmbedding(dimension=8))
    _insert_one(index)
    try:
        with pytest.raises(NativeAccelError, match="severian_native not installed"):
            index.search(_scope(), "red fox", limit=5)
    finally:
        index.close()


def test_explicit_rust_error_is_typed_not_generic(
    native_absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public error must be NativeAccelError (typed/config), which callers
    can catch — not a bare RuntimeError with no contract."""
    from severian.infrastructure.native_accel import NativeAccelError
    from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

    monkeypatch.setenv("SEVERIAN_VECTOR_BACKEND", "rust")
    index = LocalVectorIndex(tmp_path / "typed.db", HashEmbedding(dimension=8))
    _insert_one(index)
    try:
        with pytest.raises(NativeAccelError):
            index.search(_scope(), "red fox", limit=5)
    finally:
        index.close()


def test_auto_through_public_search_falls_back_with_diagnostics(
    native_absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """auto + module absent: search returns Python hits and diagnostics show
    requested=auto, selected=python and an explicit fallback reason."""
    from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

    monkeypatch.delenv("SEVERIAN_VECTOR_BACKEND", raising=False)
    index = LocalVectorIndex(tmp_path / "auto.db", HashEmbedding(dimension=8))
    _insert_one(index)
    try:
        hits = index.search(_scope(), "red fox", limit=5)
        assert [h.memory_id for h in hits] == ["m1"]
        diag = index.backend_diagnostics()
        assert diag["requested"] == "auto"
        assert diag["selected"] == "python"
        assert diag["fallback_reason"] == "severian_native not installed"
    finally:
        index.close()


def test_python_through_public_search_stays_python(
    native_absent: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """python is always python — no fallback reason, no exception."""
    from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

    monkeypatch.setenv("SEVERIAN_VECTOR_BACKEND", "python")
    index = LocalVectorIndex(tmp_path / "python.db", HashEmbedding(dimension=8))
    _insert_one(index)
    try:
        hits = index.search(_scope(), "red fox", limit=5)
        assert [h.memory_id for h in hits] == ["m1"]
        diag = index.backend_diagnostics()
        assert diag["requested"] == "python"
        assert diag["selected"] == "python"
        assert diag["fallback_reason"] is None
    finally:
        index.close()


def test_explicit_rust_execution_error_not_converted_to_python_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0-A: an explicit-rust CALL failure must propagate as NativeAccelError
    through the public path — never silently switch the index to Python."""
    import severian.infrastructure.native_accel as na
    from severian.infrastructure.native_accel import NativeAccelError
    from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

    class _BrokenNative:
        @staticmethod
        def native_version() -> str:
            return "0.1.0-test"

        @staticmethod
        def exact_top_k(query: object, rows: object, k: int) -> object:
            raise RuntimeError("kernel exploded")

    monkeypatch.setattr(na, "_NATIVE_AVAILABLE", True)
    monkeypatch.setattr(na, "_native", _BrokenNative())
    monkeypatch.setattr(na, "_native_version", "0.1.0-test")
    monkeypatch.setenv("SEVERIAN_VECTOR_BACKEND", "rust")
    index = LocalVectorIndex(tmp_path / "exec.db", HashEmbedding(dimension=8))
    _insert_one(index)
    try:
        with pytest.raises(NativeAccelError, match="kernel exploded"):
            index.search(_scope(), "red fox", limit=5)
    finally:
        index.close()
