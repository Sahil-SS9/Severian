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
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from severian.composition import ServiceBundle, build_bundle
from severian.domain.errors import NotFoundError
from severian.domain.models import Scope


def build_default_service(
    *,
    database: Path,
    fts: Path,
    vectors: Path,
    dsn: str | None = None,
) -> ServiceBundle:
    """Compose the default on-disk wiring for the CLI.

    P1-3: delegates to the shared composition root so CLI, dev launcher,
    MCP server, and API all wire through one path. The service itself is
    the canonical write/read path; the CLI only marshals flags into
    service calls. ``dsn`` switches to the PostgreSQL backend; the
    SQLite paths are still required for the lexical/vector indexes.
    """
    if dsn:
        return build_bundle(
            backend="postgresql",
            dsn=dsn,
            database=database,
            fts=fts,
            vectors=vectors,
        )
    return build_bundle(
        backend="sqlite",
        database=database,
        fts=fts,
        vectors=vectors,
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


@cli.command("export")
def export(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    output: Path = typer.Option(..., "--output", help="Destination file for the checksummed JSON snapshot."),
) -> None:
    """Export the canonical store to a portable severian-v1 snapshot.

    The snapshot is a checksummed JSON document (``export_json``) that
    ``severian import`` can restore into a fresh store. It is
    backend-portable: a SQLite export restores into PostgreSQL and
    vice versa.
    """
    bundle = build_default_service(database=database, fts=fts, vectors=vectors)
    try:
        payload = bundle.repository.export_json()
        output.parent.mkdir(parents=True, exist_ok=True)
        # P2: refuse to write through a symlink (a symlink destination
        # would let an attacker redirect the snapshot to an arbitrary
        # file). Also use an unpredictable temp name in the same
        # directory so the rename is atomic and no other process can
        # race the temporary file.
        if output.is_symlink():
            raise RuntimeError(f"refusing to export through symlink: {output}")
        temporary = output.parent / f".{output.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, output)
        _emit({"format": "severian-v1", "output": str(output), "bytes": len(payload.encode("utf-8"))})
    finally:
        bundle.close()


@cli.command("import")
def import_(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    input: Path = typer.Option(..., "--input", help="severian-v1 snapshot file to restore."),
    rebuild: bool = typer.Option(
        True,
        "--rebuild/--no-rebuild",
        help="Rebuild lexical/vector projections for imported scopes after restoring.",
    ),
) -> None:
    """Import a severian-v1 snapshot into the canonical store.

    Validates format, schema version, record shape, checksum, and
    scoped-identity conflicts before writing. Conflicting records abort
    the import; idempotent imports of the same snapshot succeed.

    By default projections are rebuilt for every scope present in the
    snapshot so the restored store is immediately searchable. Pass
    ``--no-rebuild`` to restore canonical records only.
    """
    bundle = build_default_service(database=database, fts=fts, vectors=vectors)
    try:
        payload = input.read_text(encoding="utf-8")
        bundle.repository.import_json(payload)
        touched_scopes: list[Scope] = []
        if rebuild:
            from severian.application.projections import ProjectionCoordinator

            coordinator = ProjectionCoordinator(bundle.repository, bundle.lexical, bundle.vector)
            document = json.loads(payload)
            scopes = {
                (
                    record.get("tenant_id"),
                    record.get("profile_id"),
                    record.get("collection_id"),
                    record.get("agent_id"),
                    record.get("session_id"),
                )
                for record in document.get("records", [])
                if record.get("record_type") == "memory"
                and record.get("tenant_id")
                and record.get("profile_id")
                and record.get("collection_id")
                and record.get("agent_id")
                and record.get("session_id")
            }
            for tenant, profile, collection, agent, session in scopes:
                scope = Scope(
                    tenant_id=tenant,
                    profile_id=profile,
                    collection_id=collection,
                    agent_id=agent,
                    session_id=session,
                )
                coordinator.rebuild(scope)
                touched_scopes.append(scope)
        _emit(
            {
                "format": "severian-v1",
                "input": str(input),
                "status": "imported",
                "rebuilt_scopes": [scope.model_dump() for scope in touched_scopes],
            }
        )
    except ValueError as exc:
        raise typer.BadParameter(f"cannot import snapshot: {exc}") from exc
    finally:
        bundle.close()


@cli.command("serve")
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    dsn: str | None = typer.Option(None, "--dsn", help="PostgreSQL DSN (uses PG backend when set)."),
) -> None:
    """Serve the versioned HTTP API (P2: wheel console entry)."""
    bundle = build_default_service(database=database, fts=fts, vectors=vectors, dsn=dsn)
    try:
        from severian.api.app import create_app

        app = create_app(service=bundle.service, gateway=lambda: bundle.gateway)
        import uvicorn

        uvicorn.run(app, host=host, port=port)
    finally:
        bundle.close()


if __name__ == "__main__":  # pragma: no cover
    main()
