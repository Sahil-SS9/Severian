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
