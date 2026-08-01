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

from severian.api.app import create_app
from severian.application.service import (
    BuildMemoryService,
    ContextAssembler,
    DerivationService,
    IngestionService,
    ProjectionCoordinator,
    RetrievalService,
)
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


class _HashingGateway:
    model_id = "local/dev-hasher"
    prompt_version = "dev-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]


def _start_worker(
    repository: SQLiteRepository,
    lexical: FTS5Index,
    vector: LocalVectorIndex,
    gateway: _HashingGateway,
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
    args = parser.parse_args()

    root = Path(args.storage).resolve().parent
    root.mkdir(parents=True, exist_ok=True)
    database = Path(args.storage)
    lexical = root / "severian.fts"
    vectors = root / "severian.vec"

    repository = SQLiteRepository(database)
    repository.initialize()
    lexical_idx = FTS5Index(lexical)
    vector_idx = LocalVectorIndex(vectors, HashEmbedding(dimension=256))
    service = BuildMemoryService(
        repository=repository,
        ingestion=IngestionService(repository),
        retrieval=RetrievalService(repository, lexical_idx, vector_idx),
        projection=ProjectionCoordinator(repository, lexical_idx, vector_idx),
        derivation=DerivationService(repository),
        context=ContextAssembler(),
    )
    gateway = _HashingGateway()

    _start_worker(repository, lexical_idx, vector_idx, gateway)

    app = create_app(service=service, gateway=gateway)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
