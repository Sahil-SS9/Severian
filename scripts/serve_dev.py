"""Dev launcher for the Severian HTTP API.

This is a convenience entrypoint so the API can be served locally for
the Ultramemory dashboard. It mirrors the CLI's on-disk wiring
(``build_default_service``) and calls ``create_app`` — the single
composition root. It does NOT modify the API contract.

Run:
    uv run python scripts/serve_dev.py --port 8000 --storage /tmp/severian-dev

The dashboard expects http://127.0.0.1:8000 by default.
"""
from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path
from typing import Any

from severian.api.app import create_app
from severian.application.derivation import DerivationService
from severian.application.projections import ProjectionCoordinator
from severian.auth.principal_resolver import PrincipalResolver
from severian.auth.store import ApiKeyStore
from severian.composition import build_bundle


def _start_worker(
    repository: Any,
    lexical: Any,
    vector: Any,
    gateway: Any,
    interval: float = 0.2,
) -> threading.Thread:
    """Drain pending derive jobs in a background loop.

    The API contract intentionally separates ingestion (fast, enqueues
    a durable job) from derivation (worker-owned). This loop is the
    dev-mode worker: it claims and completes derive jobs and keeps
    projections in sync so searches return freshly ingested content.
    """
    from severian.application.worker import DerivationWorker

    worker = DerivationWorker(
        repository,
        DerivationService(repository),
        ProjectionCoordinator(repository, lexical, vector),
    )

    def loop() -> None:
        while True:
            try:
                worker.drain(worker_id="dev-worker", gateway=gateway, max_jobs=50)
            except Exception:
                pass
            time.sleep(interval)

    thread = threading.Thread(target=loop, name="severian-dev-worker", daemon=True)
    thread.start()
    return thread


# -- SEC-01: non-loopback startup guard ------------------------------------

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class NonLoopbackGuardError(RuntimeError):
    """Raised when starting on a non-loopback host without auth configured."""


def is_loopback_host(host: str) -> bool:
    """True when the host is a loopback address."""
    return host.lower() in _LOOPBACK_HOSTS


def should_require_auth(*, host: str, auth_file: str | None) -> bool:
    """True when auth is required for this host.

    Loopback hosts do not require auth (dev mode). Non-loopback hosts
    require an ``--auth-file`` so the API is not exposed without
    authentication.
    """
    if is_loopback_host(host):
        return False
    return auth_file is None


def enforce_loopback_guard(*, host: str, auth_file: str | None) -> None:
    """Raise if starting on a non-loopback host without auth."""
    if should_require_auth(host=host, auth_file=auth_file):
        raise NonLoopbackGuardError(
            f"refusing to start on non-loopback host {host!r} without --auth-file: "
            "the HTTP API is fail-closed; configure an auth file with API keys "
            "before exposing on a network interface"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Severian memory API (dev).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--storage", default="/tmp/severian-dev/severian.db")
    parser.add_argument("--backend", default="sqlite", choices=["sqlite", "postgresql"])
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN (backend=postgresql)")
    parser.add_argument(
        "--auth-file",
        default=None,
        help="Path to the API-key store SQLite file. Required for non-loopback hosts.",
    )
    args = parser.parse_args()

    root = Path(args.storage).resolve().parent
    root.mkdir(parents=True, exist_ok=True)
    database = Path(args.storage)
    lexical = root / "severian.fts"
    vectors = root / "severian.vec"

    bundle = build_bundle(
        backend=args.backend,
        database=database,
        fts=lexical,
        vectors=vectors,
        dsn=args.dsn,
    )
    repository = bundle.repository
    lexical_idx = bundle.lexical
    vector_idx = bundle.vector
    gateway = bundle.gateway

    # SEC-01: refuse to start on a non-loopback host without auth.
    enforce_loopback_guard(host=args.host, auth_file=args.auth_file)

    _start_worker(repository, lexical_idx, vector_idx, gateway)

    # SEC-01: wire the principal resolver when an auth file is configured.
    principal_resolver: PrincipalResolver | None = None
    if args.auth_file:
        auth_store = ApiKeyStore(args.auth_file)
        principal_resolver = PrincipalResolver(store=auth_store)

    app = create_app(service=bundle.service, gateway=gateway, principal_resolver=principal_resolver)

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        bundle.close()


if __name__ == "__main__":
    main()
