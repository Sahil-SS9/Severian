"""Typer CLI that composes the canonical application service.

The CLI exposes:

- ``severian ingest`` — write an observation and enqueue derivation.
- ``severian search`` — query the projection through the service.

All database paths and scope dimensions are explicit. There is no
implicit environment scope. The CLI never touches the repository
or index directly: every command funnels through
:class:`severian.application.service.MemoryService`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService, MemoryService
from severian.domain.models import Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import NotFoundError, SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex


@dataclass
class ServiceBundle:
    service: MemoryService
    repository: SQLiteRepository
    lexical: FTS5Index
    vector: LocalVectorIndex
    gateway: Any

    def close(self) -> None:
        self.vector.close()
        self.lexical.close()
        self.repository.close()


def build_default_service(
    *,
    database: Path,
    fts: Path,
    vectors: Path,
) -> ServiceBundle:
    """Compose the default on-disk wiring for the CLI.

    This is the only place where the CLI may build a service. The
    service itself is the canonical write/read path; the CLI only
    marshals flags into service calls.
    """
    repository = SQLiteRepository(database)
    repository.initialize()
    lexical = FTS5Index(fts)
    vector = LocalVectorIndex(vectors, HashEmbedding(dimension=256))
    service = BuildMemoryService(
        repository=repository,
        ingestion=IngestionService(repository),
        retrieval=RetrievalService(repository, lexical, vector),
        projection=ProjectionCoordinator(repository, lexical, vector),
        derivation=DerivationService(repository),
        context=ContextAssembler(),
    )
    gateway = _HashingGateway()
    return ServiceBundle(
        service=service, repository=repository, lexical=lexical, vector=vector, gateway=gateway
    )


@dataclass
class _HashingGateway:
    """Default local derivation gateway used by the CLI.

    The CLI never talks to a network; the gateway is a deterministic
    local hash-based extractor. Tests and benchmark runners can
    substitute a different gateway via ``--gateway`` (future work).
    """

    model_id: str = "local/cli-hasher"
    prompt_version: str = "cli-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]


cli = typer.Typer(help="Severian memory platform CLI (Phase 3 vertical slice).")


def _scope_from_flags(
    *,
    tenant: str,
    profile: str,
    collection: str,
    agent: str,
    session: str,
) -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id=profile,
        collection_id=collection,
        agent_id=agent,
        session_id=session,
    )


def _emit(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True, default=str))


@cli.command("ingest")
def ingest(
    database: Path = typer.Option(..., "--database", help="SQLite canonical store path."),
    fts: Path = typer.Option(..., "--fts", help="FTS5 index database path."),
    vectors: Path = typer.Option(..., "--vectors", help="Vector index database path."),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    collection: str = typer.Option(..., "--collection"),
    agent: str = typer.Option(..., "--agent"),
    session: str = typer.Option(..., "--session"),
    content: str = typer.Option(..., "--content"),
    source: str = typer.Option("{}", "--source", help="JSON object with source metadata."),
) -> None:
    """Ingest an observation into the canonical store."""
    try:
        source_payload: dict[str, Any] = json.loads(source) if source.strip() else {}
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--source must be valid JSON: {exc}") from exc
    if not isinstance(source_payload, dict):
        raise typer.BadParameter("--source must be a JSON object")
    bundle = build_default_service(database=database, fts=fts, vectors=vectors)
    try:
        view = bundle.service.ingest_observation(
            _scope_from_flags(
                tenant=tenant,
                profile=profile,
                collection=collection,
                agent=agent,
                session=session,
            ),
            content=content,
            source=source_payload,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        bundle.close()
    _emit(
        {
            "observation_id": view.observation_id,
            "content": view.content,
            "content_hash": view.content_hash,
            "scope": view.scope.model_dump(),
            "source": view.source,
            "job_id": view.job_id,
            "job_kind": view.job_kind,
        }
    )


@cli.command("search")
def search(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    collection: str = typer.Option(..., "--collection"),
    agent: str = typer.Option(..., "--agent"),
    session: str = typer.Option(..., "--session"),
    query: str = typer.Option(..., "--query"),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
    derive: bool = typer.Option(False, "--derive", help="Run derivation before searching."),
) -> None:
    """Search the scope and emit JSON hits."""
    bundle = build_default_service(database=database, fts=fts, vectors=vectors)
    try:
        try:
            hits = bundle.service.search(
                _scope_from_flags(
                    tenant=tenant,
                    profile=profile,
                    collection=collection,
                    agent=agent,
                    session=session,
                ),
                query=query,
                limit=limit,
                derive=derive,
                gateway=bundle.gateway,
            )
        except NotFoundError:
            _emit({"hits": []})
            return
        _emit(
            {
                "hits": [
                    {
                        "memory_id": hit.memory_id,
                        "content": hit.content,
                        "source_observation_ids": list(hit.source_observation_ids),
                        "lexical_score": hit.lexical_score,
                        "vector_score": hit.vector_score,
                        "fused_score": hit.fused_score,
                        "scope": hit.scope.model_dump(),
                    }
                    for hit in hits
                ]
            }
        )
    finally:
        bundle.close()


def main() -> None:  # pragma: no cover - module entry point
    cli()


@cli.command("work")
def work(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    worker_id: str = typer.Option("worker", "--worker-id", help="Unique worker name for job claiming."),
    max_jobs: int = typer.Option(0, "--max-jobs", min=0, help="Process at most N jobs (0 = drain all pending)."),
) -> None:
    """Process pending derive jobs, then exit.

    Drains the durable derive job queue: claims pending jobs, derives
    observations into memories via the canonical derivation service,
    marks jobs done (or failed), and rebuilds projections for the
    touched scopes. Emits a JSON summary.
    """
    bundle = build_default_service(database=database, fts=fts, vectors=vectors)
    try:
        from severian.application.derivation import DerivationService
        from severian.application.projections import ProjectionCoordinator
        from severian.application.worker import DerivationWorker

        worker = DerivationWorker(
            bundle.repository,
            DerivationService(bundle.repository),
            ProjectionCoordinator(bundle.repository, bundle.lexical, bundle.vector),
        )
        summary = worker.drain(
            worker_id=worker_id,
            gateway=bundle.gateway,
            max_jobs=None if max_jobs == 0 else max_jobs,
        )
        _emit(
            {
                "processed": summary.processed,
                "failed": summary.failed,
                "touched_scopes": [scope.model_dump() for scope in summary.touched_scopes],
            }
        )
    finally:
        bundle.close()


if __name__ == "__main__":  # pragma: no cover
    main()
