"""Private Unix-socket daemon for one warm pinned Qwen embedding model.

The daemon deliberately exposes no TCP listener.  It accepts newline-delimited
JSON on an owner-only Unix socket and returns vectors from one explicitly CPU
pinned ``QwenEmbedding`` instance.  Requests and exception text are never
logged because embedding text is memory content.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import socket
import stat
import threading
from pathlib import Path
from typing import Any

from severian.application.rerank import load_reranker, rerank_max_tokens, rerank_model
from severian.infrastructure.qwen_embedding import QWEN_DIMENSION, QwenEmbedding
from severian.infrastructure.remote_qwen_embedding import RERANK_REQUEST_BYTES

MAX_RERANK_TEXTS = 100


class _StoppedDuringPreload(RuntimeError):
    """The daemon received a stop request while its model was loading."""


class EmbeddingSocketDaemon:
    """Serve one preloaded Qwen embedding provider over a private Unix socket."""

    MAX_REQUEST_BYTES = RERANK_REQUEST_BYTES
    MAX_CONCURRENT_CONNECTIONS = 16
    REQUEST_TIMEOUT_SECONDS = 2.0

    def __init__(self, socket_path: str | Path, *, embedding: Any | None = None, reranker: Any | None = None) -> None:
        path = Path(socket_path)
        if not path.is_absolute():
            raise ValueError("embedding socket path must be absolute")
        self.socket_path = path
        self.embedding = embedding if embedding is not None else QwenEmbedding(device=sidecar_device())
        self.reranker = reranker
        self._server: socket.socket | None = None
        self._socket_inode: int | None = None
        self._connection_slots = threading.BoundedSemaphore(self.MAX_CONCURRENT_CONNECTIONS)
        self._running = False

    def start(self) -> None:
        """Reserve the private socket, preload the model, then accept traffic."""
        if self._server is not None:
            raise RuntimeError("embedding socket daemon is already started")
        if not self.socket_path.parent.is_dir():
            raise ValueError("embedding socket parent directory must exist")
        self._remove_existing_socket()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Listening before the expensive preload makes a concurrent daemon
            # observe an active endpoint and fail before it loads a duplicate
            # model.  No connection is accepted until preload succeeds.
            server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            self._socket_inode = os.lstat(self.socket_path).st_ino
            server.listen(self.MAX_CONCURRENT_CONNECTIONS)
            server.settimeout(0.2)
            self._server = server
            load = getattr(self.embedding, "load", None)
            if callable(load) and load() is not True:
                raise RuntimeError("Qwen embedding model could not be preloaded")
            if self._server is not server:
                raise _StoppedDuringPreload("embedding socket daemon was stopped during preload")
        except Exception:
            if self._server is server:
                self._server = None
            server.close()
            self._remove_socket_if_owned()
            raise
        self._running = True

    def serve_forever(self) -> None:
        """Block while serving serial bounded requests until :meth:`close`."""
        if self._server is None:
            raise RuntimeError("embedding socket daemon has not been started")
        while self._running:
            try:
                connection, _ = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._running:
                    break
                raise
            if not self._connection_slots.acquire(blocking=False):
                connection.close()
                continue
            worker = threading.Thread(target=self._serve_connection, args=(connection,), daemon=True)
            worker.start()

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(self.REQUEST_TIMEOUT_SECONDS)
                self._handle_connection(connection)
        finally:
            self._connection_slots.release()

    def close(self) -> None:
        """Stop accepting traffic and remove only the daemon's own socket."""
        self._running = False
        server, self._server = self._server, None
        if server is not None:
            server.close()
        self._remove_socket_if_owned()

    def _remove_existing_socket(self) -> None:
        if not self.socket_path.exists() and not self.socket_path.is_symlink():
            return
        status = os.lstat(self.socket_path)
        if not stat.S_ISSOCK(status.st_mode):
            raise ValueError("embedding socket path already exists and is not a socket")
        if status.st_uid != os.geteuid():
            raise PermissionError("embedding socket path is not owned by this user")
        if self._socket_is_live():
            raise RuntimeError("embedding socket path is already active")
        self.socket_path.unlink()

    def _socket_is_live(self) -> bool:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.1)
            probe.connect(str(self.socket_path))
        except (OSError, TimeoutError):
            return False
        finally:
            probe.close()
        return True

    def _remove_socket_if_owned(self) -> None:
        expected_inode, self._socket_inode = self._socket_inode, None
        if expected_inode is None:
            return
        try:
            status = os.lstat(self.socket_path)
        except FileNotFoundError:
            return
        if stat.S_ISSOCK(status.st_mode) and status.st_uid == os.geteuid() and status.st_ino == expected_inode:
            self.socket_path.unlink()

    def _handle_connection(self, connection: socket.socket) -> None:
        request_line = self._read_request_line(connection)
        if request_line is None:
            return
        try:
            request = json.loads(request_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(connection, {"ok": False, "error": "invalid request"})
            return
        if not isinstance(request, dict):
            self._send(connection, {"ok": False, "error": "invalid request"})
            return

        operation = request.get("op")
        if operation == "health" and set(request) == {"op"}:
            self._send(connection, {"ok": True, "status": "ready"})
            return
        if operation == "rerank":
            self._rerank(connection, request)
            return
        text = request.get("text")
        if operation not in {"encode", "encode_query"} or not isinstance(text, str) or set(request) != {"op", "text"}:
            self._send(connection, {"ok": False, "error": "invalid request"})
            return

        try:
            vector = self.embedding.encode(text) if operation == "encode" else self.embedding.encode_query(text)
            values = self._validated_vector(vector)
            self._send(connection, {"ok": True, "vector": values})
        except Exception:
            # Do not leak model errors or supplied text over a shared boundary.
            self._send(connection, {"ok": False, "error": "embedding unavailable"})

    def _rerank(self, connection: socket.socket, request: dict[str, Any]) -> None:
        query, texts = request.get("query"), request.get("texts")
        if (
            set(request) != {"op", "query", "texts"}
            or not isinstance(query, str)
            or not isinstance(texts, list)
            or not 0 < len(texts) <= MAX_RERANK_TEXTS
            or not all(isinstance(text, str) for text in texts)
        ):
            self._send(connection, {"ok": False, "error": "invalid request"})
            return
        if self.reranker is None:
            self._send(connection, {"ok": False, "error": "rerank unavailable"})
            return
        try:
            scores = [float(score) for score in self.reranker.score(query, texts)]
            if len(scores) != len(texts) or not all(math.isfinite(score) for score in scores):
                raise ValueError("invalid rerank scores")
            self._send(connection, {"ok": True, "scores": scores})
        except Exception:
            self._send(connection, {"ok": False, "error": "rerank unavailable"})

    def _read_request_line(self, connection: socket.socket) -> bytes | None:
        chunks: list[bytes] = []
        received = 0
        try:
            while received <= self.MAX_REQUEST_BYTES:
                chunk = connection.recv(min(4096, self.MAX_REQUEST_BYTES + 1 - received))
                if not chunk:
                    return None
                received += len(chunk)
                chunks.append(chunk)
                payload = b"".join(chunks)
                newline = payload.find(b"\n")
                if newline >= 0:
                    if newline != len(payload) - 1 or received > self.MAX_REQUEST_BYTES:
                        return None
                    return payload[:-1]
        except (OSError, TimeoutError):
            return None
        return None

    @staticmethod
    def _validated_vector(raw_values: Any) -> list[float]:
        if not isinstance(raw_values, (list, tuple)) or len(raw_values) != QWEN_DIMENSION:
            raise ValueError("invalid Qwen vector")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_values):
            raise ValueError("invalid Qwen vector")
        values = [float(value) for value in raw_values]
        if not all(math.isfinite(value) for value in values):
            raise ValueError("invalid Qwen vector")
        norm = math.sqrt(math.fsum(value * value for value in values))
        if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError("invalid Qwen vector")
        return values

    @staticmethod
    def _send(connection: socket.socket, payload: dict[str, object]) -> None:
        try:
            encoded = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
            connection.sendall(encoded)
        except OSError:
            # The peer may disconnect between request validation and response.
            # This is isolated to one connection; the listening daemon remains healthy.
            return


def sidecar_device() -> str:
    """``SEVERIAN_SIDECAR_DEVICE`` (e.g. ``cuda:1``) for both shared models; the CPU when unset.

    A device that fails to load falls back to the CPU in ``load_reranker``; the embedding model raises,
    so a bad value stops the service at start-up instead of serving from the wrong place.
    """
    return os.environ.get("SEVERIAN_SIDECAR_DEVICE", "").strip() or "cpu"


def reranker_from_env() -> Any | None:
    """The shared cross-encoder named by ``SEVERIAN_RERANK``, on the sidecar device; None when unset."""
    model = rerank_model()
    return load_reranker(model, sidecar_device(), rerank_max_tokens()) if model else None


def _absolute_socket_argument(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("socket path must be absolute")
    return path


def main(argv: list[str] | None = None) -> int:
    """Run the sidecar foreground; service management owns its lifecycle."""
    parser = argparse.ArgumentParser(description="Run the private Severian Qwen embedding sidecar.")
    parser.add_argument("--socket", required=True, type=_absolute_socket_argument)
    args = parser.parse_args(argv)
    daemon = EmbeddingSocketDaemon(args.socket, reranker=reranker_from_env())

    def _stop(_signum: int, _frame: object) -> None:
        daemon.close()

    previous_sigterm = signal.signal(signal.SIGTERM, _stop)
    previous_sigint = signal.signal(signal.SIGINT, _stop)
    stopped_during_preload = False
    try:
        try:
            daemon.start()
        except _StoppedDuringPreload:
            stopped_during_preload = True
        if not stopped_during_preload:
            daemon.serve_forever()
    finally:
        daemon.close()
        signal.signal(signal.SIGTERM, previous_sigterm)
        signal.signal(signal.SIGINT, previous_sigint)
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())


__all__ = ["EmbeddingSocketDaemon", "main", "reranker_from_env", "sidecar_device"]
