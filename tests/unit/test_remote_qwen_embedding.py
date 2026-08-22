"""Shared Qwen sidecar contract.

The remote provider is intentionally Qwen-space compatible: it transports the
same pinned document/query calls over a private Unix socket and never falls
back to another vector space when the daemon is unavailable.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.infrastructure.embedding_daemon import EmbeddingSocketDaemon  # noqa: E402
from severian.infrastructure.embedding_registry import resolve_embedding_metadata  # noqa: E402
from severian.infrastructure.qwen_embedding import QWEN_DIMENSION, QwenEmbedding  # noqa: E402
from severian.infrastructure.remote_qwen_embedding import (  # noqa: E402
    RemoteEmbeddingConnectError,
    RemoteEmbeddingProtocolError,
    RemoteEmbeddingTimeoutError,
    RemoteEmbeddingTransportError,
    RemoteQwenEmbedding,
)


def _unit_vector(index: int) -> list[float]:
    values = [0.0] * QWEN_DIMENSION
    values[index] = 1.0
    return values


class _FakeEmbedding:
    dimension = QWEN_DIMENSION

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def encode(self, text: str) -> list[float]:
        self.calls.append(("encode", text))
        return _unit_vector(0)

    def encode_query(self, text: str) -> list[float]:
        self.calls.append(("encode_query", text))
        return _unit_vector(1)


@contextmanager
def _running_daemon(path: Path, embedding: _FakeEmbedding) -> Iterator[EmbeddingSocketDaemon]:
    daemon = EmbeddingSocketDaemon(path, embedding=embedding)
    daemon.start()
    thread = threading.Thread(target=daemon.serve_forever, daemon=True)
    thread.start()
    try:
        yield daemon
    finally:
        daemon.close()
        thread.join(timeout=1)


def _json_request(path: Path, request: dict[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1)
        client.connect(str(path))
        client.sendall(json.dumps(request).encode("utf-8") + b"\n")
        with client.makefile("rb") as stream:
            line = stream.readline()
    return json.loads(line.decode("utf-8"))


@contextmanager
def _scripted_server(path: Path, payload: dict[str, object]) -> Iterator[None]:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(1)

    def _serve() -> None:
        connection, _ = server.accept()
        with connection:
            with connection.makefile("rb") as stream:
                assert stream.readline()
            connection.sendall(json.dumps(payload).encode("utf-8") + b"\n")

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        thread.join(timeout=1)
        server.close()
        if path.exists():
            path.unlink()


def test_remote_selector_requires_absolute_socket_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from severian.infrastructure.embedding_resolver import (
        EmbeddingSelectionError,
        embedding_from_env,
    )

    monkeypatch.setenv("SEVERIAN_EMBEDDING", "remote-qwen")
    monkeypatch.delenv("SEVERIAN_EMBED_SOCKET", raising=False)
    with pytest.raises(EmbeddingSelectionError, match="SEVERIAN_EMBED_SOCKET"):
        embedding_from_env()

    monkeypatch.setenv("SEVERIAN_EMBED_SOCKET", "relative.sock")
    with pytest.raises(EmbeddingSelectionError, match="absolute"):
        embedding_from_env()


def test_remote_selector_returns_the_sidecar_only_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from severian.infrastructure.embedding_resolver import embedding_from_env

    monkeypatch.setenv("SEVERIAN_EMBEDDING", "remote-qwen")
    monkeypatch.setenv("SEVERIAN_EMBED_SOCKET", str(tmp_path / "embed.sock"))

    provider = embedding_from_env()

    assert isinstance(provider, RemoteQwenEmbedding)
    with pytest.raises(RemoteEmbeddingTransportError, match="requires its local sidecar"):
        provider.load()


def test_remote_refuses_a_socket_with_non_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "public.sock"
    endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    endpoint.bind(str(path))
    os.chmod(path, 0o666)
    try:
        with pytest.raises(RemoteEmbeddingTransportError, match="insecure"):
            RemoteQwenEmbedding(path).encode("never use an untrusted endpoint")
    finally:
        endpoint.close()
        if path.exists():
            path.unlink()


def test_remote_qwen_preserves_exact_qwen_space_fingerprint(tmp_path: Path) -> None:
    remote = RemoteQwenEmbedding(tmp_path / "embed.sock")

    assert resolve_embedding_metadata(remote).fingerprint == resolve_embedding_metadata(QwenEmbedding()).fingerprint


def test_unrecognised_qwen_subclass_cannot_inherit_remote_space_identity() -> None:
    class _DifferentQwenContract(QwenEmbedding):
        pass

    assert (
        resolve_embedding_metadata(_DifferentQwenContract()).fingerprint
        != resolve_embedding_metadata(QwenEmbedding()).fingerprint
    )


def test_remote_fails_loudly_when_socket_is_unavailable(tmp_path: Path) -> None:
    remote = RemoteQwenEmbedding(tmp_path / "missing.sock", timeout_seconds=0.05)

    # The subclass pins the connect failure mode; the common base still catches.
    with pytest.raises(RemoteEmbeddingConnectError, match="unavailable"):
        remote.encode("do not silently fall back")


def test_remote_unavailable_socket_still_satisfies_common_base(tmp_path: Path) -> None:
    remote = RemoteQwenEmbedding(tmp_path / "missing.sock", timeout_seconds=0.05)

    with pytest.raises(RemoteEmbeddingTransportError, match="unavailable"):
        remote.encode("do not silently fall back")


@contextmanager
def _silent_server(path: Path) -> Iterator[None]:
    """Accept one connection and never respond, forcing a client read timeout."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(1)

    def _serve() -> None:
        connection, _ = server.accept()
        time.sleep(0.5)  # hold the connection open past the client timeout
        connection.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield
    finally:
        thread.join(timeout=1)
        server.close()
        if path.exists():
            path.unlink()


def test_remote_timeout_raises_timeout_error(tmp_path: Path) -> None:
    path = tmp_path / "silent.sock"
    with _silent_server(path):
        remote = RemoteQwenEmbedding(path, timeout_seconds=0.05, max_attempts=1)
        with pytest.raises(RemoteEmbeddingTimeoutError, match="did not respond"):
            remote.encode("a sidecar that never answers")


def test_remote_health_rejects_an_invalid_ready_response(tmp_path: Path) -> None:
    path = tmp_path / "invalid-health.sock"
    with _scripted_server(path, {"ok": True, "status": "anything-else"}):
        with pytest.raises(RemoteEmbeddingProtocolError, match="invalid health"):
            RemoteQwenEmbedding(path).health()


@pytest.mark.parametrize(
    "payload, expected",
    [
        ({"ok": True, "vector": [1.0] * (QWEN_DIMENSION - 1)}, "expected 384"),
        ({"ok": True, "vector": [float("nan")] * QWEN_DIMENSION}, "non-finite"),
        ({"ok": True, "vector": [2.0] + [0.0] * (QWEN_DIMENSION - 1)}, "not normalised"),
        ({"ok": True, "wrong": _unit_vector(0)}, "missing vector"),
    ],
)
def test_remote_rejects_malformed_or_non_qwen_response(
    tmp_path: Path, payload: dict[str, object], expected: str
) -> None:
    path = tmp_path / "scripted.sock"
    with _scripted_server(path, payload):
        remote = RemoteQwenEmbedding(path)
        with pytest.raises(RemoteEmbeddingProtocolError, match=expected):
            remote.encode("validate the wire response")


def test_daemon_cli_rejects_a_relative_socket_path() -> None:
    from severian.infrastructure.embedding_daemon import main

    with pytest.raises(SystemExit) as exit_info:
        main(["--socket", "relative.sock"])

    assert exit_info.value.code == 2


def test_daemon_cli_removes_socket_when_terminated(tmp_path: Path) -> None:
    path = tmp_path / "terminated.sock"
    fake_dependency = tmp_path / "fake-dependency"
    fake_dependency.mkdir()
    (fake_dependency / "sentence_transformers.py").write_text(
        """\
class SentenceTransformer:
    def __init__(self, *_args, **_kwargs):
        pass

    def encode(self, _text, **_kwargs):
        return [1.0 / (384 ** 0.5)] * 384
"""
    )
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": f"{fake_dependency}{os.pathsep}{REPO_ROOT / 'src'}",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "severian.infrastructure.embedding_daemon", "--socket", str(path)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 3.0
        while not path.exists() and time.monotonic() < deadline:
            assert process.poll() is None
            time.sleep(0.01)
        assert path.exists()

        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3.0)
        assert process.returncode == 0, stdout + stderr
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=3.0)

    assert not path.exists()


def test_daemon_refuses_a_concurrent_start_before_duplicate_preload(tmp_path: Path) -> None:
    class _BlockingLoadEmbedding(_FakeEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.load_started = threading.Event()
            self.release_load = threading.Event()

        def load(self) -> bool:
            self.load_started.set()
            assert self.release_load.wait(timeout=1)
            return True

    class _LoadCountingEmbedding(_FakeEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.load_calls = 0

        def load(self) -> bool:
            self.load_calls += 1
            return True

    path = tmp_path / "starting.sock"
    first_embedding = _BlockingLoadEmbedding()
    first = EmbeddingSocketDaemon(path, embedding=first_embedding)
    start_errors: list[BaseException] = []

    def _start_first() -> None:
        try:
            first.start()
        except BaseException as exc:  # assertion below makes thread failures visible
            start_errors.append(exc)

    starter = threading.Thread(target=_start_first)
    starter.start()
    assert first_embedding.load_started.wait(timeout=1)

    replacement = _LoadCountingEmbedding()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            EmbeddingSocketDaemon(path, embedding=replacement).start()
        assert replacement.load_calls == 0
    finally:
        first_embedding.release_load.set()
        starter.join(timeout=1)
        first.close()

    assert not starter.is_alive()
    assert start_errors == []


def test_daemon_stops_if_closed_during_preload(tmp_path: Path) -> None:
    class _BlockingLoadEmbedding(_FakeEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.load_started = threading.Event()
            self.release_load = threading.Event()

        def load(self) -> bool:
            self.load_started.set()
            assert self.release_load.wait(timeout=1)
            return True

    path = tmp_path / "interrupted.sock"
    embedding = _BlockingLoadEmbedding()
    daemon = EmbeddingSocketDaemon(path, embedding=embedding)
    errors: list[BaseException] = []

    def _start() -> None:
        try:
            daemon.start()
        except BaseException as exc:  # assertion below makes thread failures visible
            errors.append(exc)

    starter = threading.Thread(target=_start)
    starter.start()
    assert embedding.load_started.wait(timeout=1)
    daemon.close()
    assert not path.exists()

    embedding.release_load.set()
    starter.join(timeout=1)
    daemon.close()

    assert not starter.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "stopped during preload" in str(errors[0])
    assert not path.exists()


def test_daemon_refuses_to_replace_a_live_owned_socket_before_preloading(tmp_path: Path) -> None:
    class _LoadCountingEmbedding(_FakeEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.load_calls = 0

        def load(self) -> bool:
            self.load_calls += 1
            return True

    path = tmp_path / "live.sock"
    first = EmbeddingSocketDaemon(path, embedding=_FakeEmbedding())
    first.start()
    replacement = _LoadCountingEmbedding()
    try:
        with pytest.raises(RuntimeError, match="already active"):
            EmbeddingSocketDaemon(path, embedding=replacement).start()
        assert replacement.load_calls == 0
    finally:
        first.close()


def test_daemon_close_preserves_a_replacement_socket(tmp_path: Path) -> None:
    path = tmp_path / "replaced.sock"
    daemon = EmbeddingSocketDaemon(path, embedding=_FakeEmbedding())
    daemon.start()
    path.unlink()
    replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    replacement.bind(str(path))
    os.chmod(path, 0o600)
    replacement_inode = os.stat(path).st_ino
    try:
        daemon.close()
        assert path.exists()
        assert os.stat(path).st_ino == replacement_inode
    finally:
        replacement.close()
        if path.exists():
            path.unlink()


def test_daemon_health_doc_and_query_roundtrip_keep_private_socket(tmp_path: Path) -> None:
    path = tmp_path / "severian-embed.sock"
    fake = _FakeEmbedding()

    with _running_daemon(path, fake):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert _json_request(path, {"op": "health"}) == {"ok": True, "status": "ready"}

        remote = RemoteQwenEmbedding(path)
        assert remote.health() is True
        assert remote.encode("document") == _unit_vector(0)
        assert remote.encode_query("question") == _unit_vector(1)
        assert fake.calls == [("encode", "document"), ("encode_query", "question")]

    assert not path.exists()


def test_daemon_serves_parallel_clients_without_serial_timeout(tmp_path: Path) -> None:
    class _SlowEmbedding(_FakeEmbedding):
        def encode(self, text: str) -> list[float]:
            time.sleep(0.3)
            return super().encode(text)

    path = tmp_path / "parallel.sock"
    embedding = _SlowEmbedding()
    with _running_daemon(path, embedding):
        barrier = threading.Barrier(2)
        results: list[list[float]] = []
        errors: list[BaseException] = []

        def _embed(text: str) -> None:
            try:
                barrier.wait(timeout=1)
                results.append(RemoteQwenEmbedding(path, timeout_seconds=0.45).encode(text))
            except BaseException as exc:  # assertion below makes thread failures visible
                errors.append(exc)

        first = threading.Thread(target=_embed, args=("one",))
        second = threading.Thread(target=_embed, args=("two",))
        first.start()
        second.start()
        first.join(timeout=1)
        second.join(timeout=1)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert results == [_unit_vector(0), _unit_vector(0)]
        assert embedding.calls == [("encode", "one"), ("encode", "two")] or embedding.calls == [
            ("encode", "two"),
            ("encode", "one"),
        ]


def test_daemon_rejects_connections_above_its_bounded_parallelism(tmp_path: Path) -> None:
    class _BlockingEmbedding(_FakeEmbedding):
        def __init__(self) -> None:
            super().__init__()
            self.release = threading.Event()

        def encode(self, text: str) -> list[float]:
            self.calls.append(("encode", text))
            assert self.release.wait(timeout=2)
            return _unit_vector(0)

    path = tmp_path / "bounded-parallel.sock"
    embedding = _BlockingEmbedding()
    clients: list[socket.socket] = []
    with _running_daemon(path, embedding):
        try:
            for _ in range(EmbeddingSocketDaemon.MAX_CONCURRENT_CONNECTIONS):
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                client.settimeout(1)
                client.connect(str(path))
                client.sendall(b'{"op":"encode","text":"hold"}\n')
                client.shutdown(socket.SHUT_WR)
                clients.append(client)

            deadline = time.monotonic() + 1
            while (
                len(embedding.calls) < EmbeddingSocketDaemon.MAX_CONCURRENT_CONNECTIONS
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert len(embedding.calls) == EmbeddingSocketDaemon.MAX_CONCURRENT_CONNECTIONS

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as overflow:
                overflow.settimeout(1)
                overflow.connect(str(path))
                overflow.sendall(b'{"op":"health"}\n')
                overflow.shutdown(socket.SHUT_WR)
                try:
                    received = overflow.recv(1)
                except (BrokenPipeError, ConnectionResetError):
                    received = b""
                assert received == b""
        finally:
            embedding.release.set()
            for client in clients:
                client.close()


def test_daemon_ignores_a_disconnected_client_during_response() -> None:
    server, client = socket.socketpair()
    try:
        client.close()
        EmbeddingSocketDaemon._send(server, {"ok": False, "error": "invalid request"})
    finally:
        server.close()


def test_daemon_rejects_invalid_operations_and_text_without_echoing_text(tmp_path: Path) -> None:
    path = tmp_path / "invalid.sock"
    fake = _FakeEmbedding()

    with _running_daemon(path, fake):
        assert _json_request(path, {"op": "delete", "text": "PRIVATE-MEMORY-TEXT"}) == {
            "ok": False,
            "error": "invalid request",
        }
        assert _json_request(path, {"op": "encode", "text": 42}) == {
            "ok": False,
            "error": "invalid request",
        }

    assert fake.calls == []


def test_daemon_rejects_oversized_request_without_echoing_text(tmp_path: Path) -> None:
    path = tmp_path / "bounded.sock"
    fake = _FakeEmbedding()

    with _running_daemon(path, fake):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            client.connect(str(path))
            client.sendall(b"x" * (EmbeddingSocketDaemon.MAX_REQUEST_BYTES + 1))
            client.shutdown(socket.SHUT_WR)
            assert client.recv(1) == b""

    assert fake.calls == []
