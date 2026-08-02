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


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Severian memory API (dev).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--storage", default="/tmp/severian-dev/severian.db")
    parser.add_argument("--backend", default="sqlite", choices=["sqlite", "postgresql"])
    parser.add_argument("--dsn", default=None, help="PostgreSQL DSN (backend=postgresql)")
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

    _start_worker(repository, lexical_idx, vector_idx, gateway)

    app = create_app(service=bundle.service, gateway=gateway)

    import uvicorn

    try:
        uvicorn.run(app, host=args.host, port=args.port)
    finally:
        bundle.close()


if __name__ == "__main__":
    main()
