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
            embedding=_embedding_from_env(),
        )
    return build_bundle(
        backend="sqlite",
        database=database,
        fts=fts,
        vectors=vectors,
        embedding=_embedding_from_env(),
    )


def _embedding_from_env() -> Any:
    """Honor the same SEVERIAN_EMBEDDING contract as the Hermes adapter.

    Delegates to the single central resolver (zero-debt plan 2.2) — the
    CLI runs ``severian work`` which REBUILDS vector projections for
    touched scopes, so the selected space MUST match the gateways'.
    The resolver returns the hash embedding explicitly when unset (no
    None + composition default split-brain), and the selected pinned Qwen
    provider when ``SEVERIAN_EMBEDDING=qwen`` — with NO silent hash
    substitution. ``granite`` remains an explicit legacy selector for
    migration and rollback. Imported lazily so the CLI stays fast and
    dependency-light when a semantic provider is not configured.
    """
    from severian.infrastructure.embedding_resolver import embedding_from_env

    return embedding_from_env()


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


def _system_scope() -> Scope:
    """Reserved all-sentinel scope for store-wide administrative actions.

    Export/import/backup are store-wide operations with no tenant context;
    their audit trail lives under this sentinel scope (SEC-02: admin
    operations must be auditable).
    """
    return Scope(tenant_id="_system", profile_id="_", collection_id="_", agent_id="_", session_id="_")


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
        from severian.application.worker import DerivationWorker

        # ARCH-05: use the canonical derivation + projection from the
        # composition root. Previously this reconstructed DerivationService
        # WITHOUT the graph extractor, so CLI-derived memories diverged
        # from service-derived ones.
        worker = DerivationWorker(
            bundle.repository,
            bundle.derivation,
            bundle.projection,
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
                "projection_failed": summary.projection_failed,
                "touched_scopes": [scope.model_dump() for scope in summary.touched_scopes],
            }
        )
    finally:
        bundle.close()


@cli.command("dead-letter")
def dead_letter(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    requeue: str = typer.Option("", "--requeue", help="Job ID to requeue (recover) back to pending."),
) -> None:
    """Inspect dead-lettered jobs, or requeue one for recovery (OPS-07).

    With no --requeue, lists all dead-lettered jobs as JSON. With
    --requeue <job-id>, resets that job to pending with a fresh attempt
    budget so the next ``work`` pass retries it.
    """
    bundle = build_default_service(database=database, fts=fts, vectors=vectors)
    if requeue:
        job = bundle.repository.requeue_job(requeue)
        _emit({"action": "requeue", "job_id": job.id, "status": job.status})
        return
    dead = bundle.repository.list_dead_letter_jobs()
    _emit(
        {
            "dead_letter_count": len(dead),
            "jobs": [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "scope": j.scope.model_dump(),
                    "attempts": j.attempts,
                    "last_error": j.last_error,
                }
                for j in dead
            ],
        }
    )


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
        # SEC-04: enforce the approved-path policy before creating any
        # output parent directory — a symlinked ancestor or a path
        # outside the approved roots must be rejected before mkdir.
        from severian.cli.path_policy import check_write_destination

        safe_output = check_write_destination(output, purpose="export")
        safe_output.parent.mkdir(parents=True, exist_ok=True)
        temporary = safe_output.parent / f".{safe_output.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        temporary.write_text(payload, encoding="utf-8")
        # SEC-03: the temp snapshot contains the full multi-scope store —
        # never leave it world-readable (default umask 0644).
        os.chmod(temporary, 0o600)
        os.replace(temporary, safe_output)
        # SEC-02: record the admin action in the audit trail.
        try:
            bundle.repository.append_audit(
                _system_scope(), "store.export", str(safe_output), {"bytes": len(payload.encode("utf-8"))}
            )
        except Exception:
            pass  # audit failure must not fail the export itself
        _emit({"format": "severian-v1", "output": str(safe_output), "bytes": len(payload.encode("utf-8"))})
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
        # SEC-04: the snapshot source must be an approved regular file —
        # a symlinked input could redirect the import to an arbitrary
        # payload the operator did not intend to restore.
        from severian.cli.path_policy import check_read_source

        safe_input = check_read_source(input, purpose="import")
        payload = safe_input.read_text(encoding="utf-8")
        bundle.repository.import_json(payload)
        touched_scopes: list[Scope] = []
        rebuild_error: str | None = None
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
                try:
                    coordinator.rebuild(scope)
                    touched_scopes.append(scope)
                except Exception as exc:  # noqa: BLE001 — report, do not mask
                    # DATA-02: the canonical import is committed and atomic,
                    # but projections are now stale for this scope. Surface
                    # the partial state explicitly so the operator can
                    # re-run `severian import --rebuild`.
                    rebuild_error = f"{scope.model_dump()}: {exc}"
        # SEC-02: record the admin action in the audit trail.
        try:
            bundle.repository.append_audit(
                _system_scope(),
                "store.import",
                str(safe_input),
                {"rebuilt_scopes": len(touched_scopes)},
            )
        except Exception:
            pass  # audit failure must not fail the import itself
        _emit(
            {
                "format": "severian-v1",
                "input": str(safe_input),
                "status": "imported",
                "rebuilt_scopes": [scope.model_dump() for scope in touched_scopes],
                "rebuild_error": rebuild_error,
            }
        )
        if rebuild_error is not None:
            # DATA-02: the canonical import succeeded atomically, but at
            # least one scope's projections could not be rebuilt. Make the
            # partial state loud — the operator must re-run with --rebuild.
            typer.echo(
                f"WARNING: canonical store imported, but projections are stale "
                f"for scope(s): {rebuild_error}; re-run `severian import --rebuild`",
                err=True,
            )
    except ValueError as exc:
        raise typer.BadParameter(f"cannot import snapshot: {exc}") from exc
    finally:
        bundle.close()


@cli.command("rebuild-vectors")
def rebuild_vectors(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    tenant: str | None = typer.Option(None, "--tenant"),
    profile: str | None = typer.Option(None, "--profile"),
    collection: str | None = typer.Option(None, "--collection"),
    agent: str | None = typer.Option(None, "--agent"),
    session: str | None = typer.Option(None, "--session"),
    all_scopes: bool = typer.Option(
        False,
        "--all-scopes",
        help="Rebuild every active canonical-memory scope in one projection swap.",
    ),
    dsn: str | None = typer.Option(None, "--dsn"),
    keep_rollback: bool = typer.Option(
        True,
        "--keep-rollback/--no-keep-rollback",
        help="Keep the pre-swap index as rollback until explicit cleanup.",
    ),
) -> None:
    """Rebuild vector projections side-by-side, verify, then swap atomically.

    This is an operator maintenance command.  Unlike normal CLI commands it
    opens only canonical storage: a legacy or mismatched projection is the
    object being replaced and must never be opened before the side-by-side
    rebuild begins.  ``--all-scopes`` is required when a shared projection
    holds multiple memory scopes.
    """
    del fts  # Retained as an explicit legacy CLI flag; vector rebuild does not open lexical projections.
    from severian.infrastructure.embedding_resolver import embedding_from_env
    from severian.infrastructure.rebuild import rebuild_pg_vectors, rebuild_sqlite_vectors

    scope_values = {
        "tenant": tenant,
        "profile": profile,
        "collection": collection,
        "agent": agent,
        "session": session,
    }
    if all_scopes:
        supplied = [name for name, value in scope_values.items() if value is not None]
        if supplied:
            raise typer.BadParameter("--all-scopes cannot be combined with individual scope flags")
        scope: Scope | None = None
    else:
        missing = [name for name, value in scope_values.items() if not value]
        if missing:
            raise typer.BadParameter(
                "individual rebuild requires all scope flags; use --all-scopes for the complete projection"
            )
        scope = _scope_from_flags(
            tenant=tenant or "",
            profile=profile or "",
            collection=collection or "",
            agent=agent or "",
            session=session or "",
        )

    repository: Any
    if dsn:
        from severian.infrastructure.postgresql import PostgreSQLBackend

        repository = PostgreSQLBackend(dsn=dsn)
    else:
        from severian.infrastructure.sqlite import SQLiteRepository

        repository = SQLiteRepository(database)
    repository.initialize()
    try:
        # Rebuilds are destructive projection swaps: the embedding MUST be
        # stated explicitly (fail closed on unset/hash — the 2026-08-22
        # live-index incident class).
        embedding = embedding_from_env(require_explicit=True)
        if dsn:
            result = rebuild_pg_vectors(dsn, repository, scope, embedding, keep_rollback=keep_rollback)
        else:
            result = rebuild_sqlite_vectors(vectors, repository, scope, embedding, keep_rollback=keep_rollback)
        payload: dict[str, object] = {
            "format": "severian-v1",
            "status": result.status,
            "provider_id": result.provider_id,
            "scope": "all-active-memory-scopes" if scope is None else str(scope),
        }
        if result.verification is not None:
            payload["verification"] = {
                "canonical_count": result.verification.canonical_count,
                "vector_count": result.verification.vector_count,
                "digest_match": result.verification.digest_match,
                "dimension": result.verification.dimension,
                "fingerprint": result.verification.fingerprint,
                "canary_hits": result.verification.canary_hits,
                "canary_total": result.verification.canary_total,
                "deterministic": result.verification.deterministic,
            }
        if result.rollback_path:
            payload["rollback_path"] = result.rollback_path
        if result.error:
            payload["error"] = result.error
        _emit(payload)
        if result.status != "swapped":
            raise typer.Exit(code=1)
    finally:
        repository.close()


@cli.command("rollback")
def rollback(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    collection: str = typer.Option(..., "--collection"),
    agent: str = typer.Option(..., "--agent"),
    session: str = typer.Option(..., "--session"),
    revision_id: str = typer.Option(..., "--revision-id", help="Historical revision to restore as active head."),
    dsn: str | None = typer.Option(None, "--dsn"),
) -> None:
    """Restore a historical memory revision as the active head (A-2)."""
    bundle = build_default_service(database=database, fts=fts, vectors=vectors, dsn=dsn)
    try:
        scope = _scope_from_flags(tenant=tenant, profile=profile, collection=collection, agent=agent, session=session)
        restored = bundle.service.rollback_memory(scope, revision_id)
        _emit(
            {
                "format": "severian-v1",
                "status": "rolled_back",
                "revision_id": restored.id,
                "content": restored.content,
                "superseded_by_id": restored.superseded_by_id,
            }
        )
    except Exception as exc:
        raise typer.BadParameter(f"cannot rollback: {exc}") from exc
    finally:
        bundle.close()


@cli.command("episode")
def episode(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    collection: str = typer.Option(..., "--collection"),
    agent: str = typer.Option(..., "--agent"),
    session: str = typer.Option(..., "--session"),
    dsn: str | None = typer.Option(None, "--dsn"),
) -> None:
    """Consolidate a session's observations into an episode memory (B-2)."""
    bundle = build_default_service(database=database, fts=fts, vectors=vectors, dsn=dsn)
    try:
        scope = _scope_from_flags(tenant=tenant, profile=profile, collection=collection, agent=agent, session=session)
        from severian.application.episodes import EpisodeService

        episode_memory = EpisodeService(bundle.repository).consolidate_session(scope)
        if episode_memory is None:
            _emit({"format": "severian-v1", "status": "skipped", "reason": "below min observations"})
        else:
            _emit(
                {
                    "format": "severian-v1",
                    "status": "episode_created",
                    "memory_id": episode_memory.id,
                    "kind": episode_memory.kind,
                    "source_observation_count": len(episode_memory.source_observation_ids),
                    "content": episode_memory.content,
                }
            )
    except Exception as exc:
        raise typer.BadParameter(f"cannot consolidate episode: {exc}") from exc
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
