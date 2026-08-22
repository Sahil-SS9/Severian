"""Fail-closed Unix-socket client for the pinned Qwen embedding space.

``RemoteQwenEmbedding`` deliberately subclasses :class:`QwenEmbedding` so the
embedding registry recognises it as the exact same pinned Qwen vector space.
Only the transport changes: vectors are produced by the local sidecar, not by a
second in-process model.  A transport error is never converted to another
embedding provider.
"""

from __future__ import annotations

import json
import math
import os
import socket
import stat
import time
from pathlib import Path
from typing import Any

from severian.infrastructure.qwen_embedding import QwenEmbedding

MAX_REQUEST_BYTES = 64 * 1024
# Encode cost scales linearly with characters (~1.3ms/char pinned CPU): texts
# beyond this cap cannot complete inside any sane socket timeout, so they are
# rejected up front instead of wedging a drain pass into dead-letter territory.
MAX_TEXT_CHARS = 8000
# Linear backoff between transport retries: 0.25s, 0.5s, 0.75s, ...
_RETRY_BACKOFF_SECONDS = 0.25


class RemoteEmbeddingTransportError(RuntimeError):
    """The required local embedding sidecar is unavailable or invalid."""


class RemoteEmbeddingTextTooLongError(ValueError):
    """The supplied text exceeds the sidecar's encodable length contract."""


def _validated_absolute_socket_path(socket_path: str | Path) -> Path:
    path = Path(socket_path)
    if not path.is_absolute():
        raise ValueError("remote Qwen embedding socket path must be absolute")
    return path


class RemoteQwenEmbedding(QwenEmbedding):
    """The exact Qwen embedding contract served by one local sidecar.

    Metadata is inherited from :class:`QwenEmbedding`, intentionally preserving
    the existing index fingerprint.  This provider never calls the inherited
    local model loader or its explicit hash fallback.
    """

    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout_seconds: float = 5.0,
        max_attempts: int = 3,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
            raise ValueError("remote Qwen timeout_seconds must be finite and positive")
        if not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("remote Qwen max_attempts must be a positive integer")
        super().__init__()
        self.socket_path = _validated_absolute_socket_path(socket_path)
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    def load(self) -> bool:
        """Refuse a local model load: this provider is sidecar-only."""
        raise RemoteEmbeddingTransportError("remote Qwen embedding requires its local sidecar")

    def or_hash(self) -> QwenEmbedding:
        """Reject the inherited explicit hash escape hatch for this selector."""
        raise RemoteEmbeddingTransportError("remote Qwen embedding cannot fall back to hash")

    def health(self) -> bool:
        response = self._request({"op": "health"})
        if response.get("status") != "ready":
            raise RemoteEmbeddingTransportError("remote Qwen sidecar returned an invalid health response")
        return True

    def encode(self, text: str) -> list[float]:
        return self._encode("encode", text)

    def encode_query(self, text: str) -> list[float]:
        return self._encode("encode_query", text)

    def _encode(self, operation: str, text: str) -> list[float]:
        if not isinstance(text, str):
            raise TypeError("remote Qwen embedding text must be a string")
        if len(text) > MAX_TEXT_CHARS:
            raise RemoteEmbeddingTextTooLongError(
                f"remote Qwen embedding text exceeds {MAX_TEXT_CHARS} characters "
                f"(got {len(text)}); truncate before encoding"
            )
        response = self._request({"op": operation, "text": text})
        return self._validated_vector(response)

    def _request(self, payload: dict[str, str]) -> dict[str, Any]:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:  # defensive: payload is internal
            raise RemoteEmbeddingTransportError("remote Qwen request could not be encoded") from exc
        if len(encoded) > MAX_REQUEST_BYTES:
            raise RemoteEmbeddingTransportError("remote Qwen request exceeds the sidecar limit")
        self._require_private_socket()

        last_exc: Exception | None = None
        for _attempt in range(self.max_attempts):
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(self.timeout_seconds)
                    client.connect(str(self.socket_path))
                    client.sendall(encoded)
                    client.shutdown(socket.SHUT_WR)
                    with client.makefile("rb") as stream:
                        response_line = stream.readline(MAX_REQUEST_BYTES + 1)
                break
            except (OSError, TimeoutError) as exc:
                last_exc = exc
                # Transient contention on the single sidecar (burst drains,
                # gateway traffic) surfaces as a timeout; a short backoff and
                # retry absorbs it instead of failing the whole pass. The
                # socket-permission guard above already ran, so retries only
                # cover genuine transport transience.
                if _attempt < self.max_attempts - 1:
                    time.sleep(_RETRY_BACKOFF_SECONDS * (_attempt + 1))
        else:
            raise RemoteEmbeddingTransportError(
                "remote Qwen sidecar is unavailable"
            ) from last_exc

        if not response_line or len(response_line) > MAX_REQUEST_BYTES or not response_line.endswith(b"\n"):
            raise RemoteEmbeddingTransportError("remote Qwen sidecar returned an invalid response")
        try:
            response = json.loads(response_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteEmbeddingTransportError("remote Qwen sidecar returned invalid JSON") from exc
        if not isinstance(response, dict):
            raise RemoteEmbeddingTransportError("remote Qwen sidecar returned an invalid response")
        if response.get("ok") is not True:
            raise RemoteEmbeddingTransportError("remote Qwen sidecar rejected the request")
        return response

    def _require_private_socket(self) -> None:
        try:
            status = os.lstat(self.socket_path)
        except FileNotFoundError:
            # Preserve the normal transport-unavailable failure for a daemon
            # that has not started or has already shut down.
            return
        if not stat.S_ISSOCK(status.st_mode):
            raise RemoteEmbeddingTransportError("remote Qwen socket path is insecure")
        if status.st_uid != os.geteuid() or stat.S_IMODE(status.st_mode) != 0o600:
            raise RemoteEmbeddingTransportError("remote Qwen socket permissions are insecure")

    def _validated_vector(self, response: dict[str, Any]) -> list[float]:
        if "vector" not in response:
            raise RemoteEmbeddingTransportError("remote Qwen sidecar response is missing vector")
        raw_values = response["vector"]
        if not isinstance(raw_values, list) or len(raw_values) != self.dimension:
            actual = len(raw_values) if isinstance(raw_values, list) else "non-list"
            raise RemoteEmbeddingTransportError(
                f"remote Qwen sidecar returned {actual} dims, expected {self.dimension}"
            )
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw_values):
            raise RemoteEmbeddingTransportError("remote Qwen sidecar vector has non-numeric values")
        values = [float(value) for value in raw_values]
        if not all(math.isfinite(value) for value in values):
            raise RemoteEmbeddingTransportError("remote Qwen sidecar vector contains non-finite values")
        try:
            norm = math.sqrt(math.fsum(value * value for value in values))
        except OverflowError as exc:
            raise RemoteEmbeddingTransportError("remote Qwen sidecar vector norm overflowed") from exc
        if not math.isfinite(norm) or not math.isclose(norm, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise RemoteEmbeddingTransportError("remote Qwen sidecar vector is not normalised")
        return values


__all__ = [
    "MAX_REQUEST_BYTES",
    "MAX_TEXT_CHARS",
    "RemoteEmbeddingTextTooLongError",
    "RemoteEmbeddingTransportError",
    "RemoteQwenEmbedding",
]
