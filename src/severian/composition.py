"""Shared, config-driven composition root for every Severian runner.

P1-3: one place builds the service bundle for API / CLI / dev / MCP /
Hermes / worker. Runners must not wire their own repository + indexes;
they call :func:`build_bundle` with a backend name and let this module
own the composition. This keeps SQLite and PostgreSQL on the same
semantic path and makes the Postgres deployment a first-class option
instead of a library-only fixture.

Usage::

    bundle = build_bundle(
        backend="sqlite",
        database=Path("/data/severian.db"),
        fts=Path("/data/severian.fts"),
        vectors=Path("/data/severian.vec"),
    )
    # bundle.service / bundle.repository / bundle.lexical / bundle.vector
    bundle.close()
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from severian.application.derivation import DerivationService
from severian.application.extraction import EntityExtractionService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService, MemoryService
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.vector import LocalVectorIndex


@dataclass
class ServiceBundle:
    """Everything a runner needs, with a single teardown path."""

    service: MemoryService
    repository: Any
    lexical: FTS5Index
    vector: Any
    gateway: Any
    projection: Any
    derivation: Any
    backend: str = "sqlite"

    def close(self) -> None:
        self.vector.close()
        self.lexical.close()
        close = getattr(self.repository, "close", None)
        if close is not None:
            close()


class _HashingGateway:
    """Deterministic local derivation gateway (no network).

    Used by CLI/dev/MCP/worker runners. Tests and benchmarks substitute
    their own gateway at the service boundary.
    """

    model_id = "local/hashing"
    prompt_version = "prompt-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]


def build_bundle(
    *,
    backend: str = "sqlite",
    database: str | Path | None = None,
    fts: str | Path | None = None,
    vectors: str | Path | None = None,
    dsn: str | None = None,
    gateway: object | None = None,
    embedding: Any | None = None,
) -> ServiceBundle:
    """Compose the canonical service bundle for the requested backend.

    ``sqlite`` requires ``database``/``fts``/``vectors`` paths (an
    in-memory store is used when omitted, suitable for tests).
    ``postgresql`` requires ``dsn``; SQLite index files are still used
    for the lexical index (FTS5 has no PG equivalent) while the vector
    projection uses pgvector when ``pg_vectors=True`` — see
    :func:`build_bundle` callers for production wiring.

    ``embedding`` accepts any :class:`EmbeddingProvider` (e.g. Granite
    R2). When omitted, the deterministic :class:`HashEmbedding` is used
    — the default is always safe and dependency-free.
    """
    backend = (backend or "sqlite").lower()
    if backend == "sqlite":
        return _build_sqlite(
            database=database, fts=fts, vectors=vectors, gateway=gateway, embedding=embedding
        )
    if backend in {"postgresql", "postgres"}:
        return _build_postgresql(dsn=dsn, gateway=gateway, embedding=embedding)
    raise ValueError(f"unknown backend: {backend!r} (expected sqlite or postgresql)")


def _compose(
    *,
    repository: Any,
    lexical: FTS5Index,
    vector: Any,
    gateway: Any,
    backend: str,
) -> ServiceBundle:
    projection = ProjectionCoordinator(repository, lexical, vector)
    extraction = EntityExtractionService(repository)
    derivation = DerivationService(repository, extraction=extraction)
    service = BuildMemoryService(
        repository=repository,
        ingestion=IngestionService(repository),
        retrieval=RetrievalService(repository, lexical, vector),
        projection=projection,
        derivation=derivation,
        context=ContextAssembler(),
    )
    return ServiceBundle(
        service=service,
        repository=repository,
        lexical=lexical,
        vector=vector,
        projection=projection,
        derivation=derivation,
        gateway=gateway or _HashingGateway(),
        backend=backend,
    )


def _build_sqlite(
    *,
    database: str | Path | None,
    fts: str | Path | None,
    vectors: str | Path | None,
    gateway: Any | None,
    embedding: Any | None = None,
) -> ServiceBundle:
    import tempfile

    from severian.infrastructure.sqlite import SQLiteRepository

    if database is None:
        database = Path(tempfile.mkdtemp(prefix="severian-")) / "severian.db"
    if fts is None:
        fts = Path(str(database)).with_suffix(".fts")
    if vectors is None:
        vectors = Path(str(database)).with_suffix(".vec")
    repository = SQLiteRepository(database)
    repository.initialize()
    lexical = FTS5Index(fts)
    from severian.infrastructure.embedding_resolver import resolve_embedding

    vector = LocalVectorIndex(vectors, embedding or resolve_embedding("hash"))
    return _compose(repository=repository, lexical=lexical, vector=vector, gateway=gateway, backend="sqlite")


def _build_postgresql(
    *,
    dsn: str | None,
    gateway: Any | None,
    embedding: Any | None = None,
) -> ServiceBundle:
    import tempfile

    if not dsn or not dsn.strip():
        raise ValueError("postgresql backend requires a DSN")
    from severian.infrastructure.pgvector_index import PGVectorIndex
    from severian.infrastructure.postgresql import PostgreSQLBackend

    repository = PostgreSQLBackend(dsn=dsn)
    repository.initialize()
    repository.verify_schema()  # P1-3: fail startup on schema mismatch
    vector = PGVectorIndex(dsn, embedding)
    vector.initialize()
    scratch = Path(tempfile.mkdtemp(prefix="severian-fts-"))
    lexical = FTS5Index(scratch / "severian.fts")
    return _compose(repository=repository, lexical=lexical, vector=vector, gateway=gateway, backend="postgresql")


__all__ = ["ServiceBundle", "build_bundle"]
