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
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from severian.application.dreaming import (
    ConsolidationService,
    ContradictionScanService,
    CurationService,
    RetentionPolicyService,
)
from severian.application.dreamqueue import DREAM_MODES, dreaming_enabled
from severian.application.dreamwriter import DreamWriter
from severian.application.eventtime import parse_time
from severian.application.markers import MARKER_KINDS
from severian.application.service import SEARCH_VIEWS
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
migrate_app = typer.Typer(help="Additive, rehearsed store migrations.")
cli.add_typer(migrate_app, name="migrate")
jobs_app = typer.Typer(help="Recover queued jobs that ended failed or dead-lettered.")
cli.add_typer(jobs_app, name="jobs")


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


@migrate_app.command("message-units")
def migrate_message_units_command(
    database: Path = typer.Option(..., "--database", help="SQLite canonical store path."),
    fts: Path = typer.Option(..., "--fts", help="FTS5 index database path."),
    vectors: Path = typer.Option(..., "--vectors", help="Vector index database path."),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    collection: str = typer.Option(..., "--collection"),
    agent: str = typer.Option(..., "--agent"),
    session: str = typer.Option(..., "--session"),
    confirm: bool = typer.Option(False, "--confirm", help="Apply after backup and rehearsal; default is a dry run."),
    backup_dir: Path | None = typer.Option(None, "--backup-dir", help="Where to put the SQLite backup."),
) -> None:
    """Backfill message units: backup -> integrity -> rehearsal -> apply -> rebuild."""
    from severian.application.message_units import migrate_message_units

    scope = _scope_from_flags(
        tenant=tenant,
        profile=profile,
        collection=collection,
        agent=agent,
        session=session,
    )
    backup: Path | None = None
    integrity: str | None = None
    rehearsal: dict[str, Any] | None = None
    if confirm:
        backup = _sqlite_backup(database, backup_dir)
        integrity = _integrity_check(backup)
        if integrity != "ok":
            raise typer.BadParameter(f"backup integrity check failed: {integrity}")
        rehearsal = _rehearse_message_units(database, fts, vectors, scope)
    bundle = build_default_service(database=database, fts=fts, vectors=vectors)
    try:
        report = migrate_message_units(bundle.repository, scope, confirm=confirm, rebuild=bundle.projection)
    finally:
        bundle.close()
    payload = report.as_dict()
    payload.update({"backup": str(backup) if backup else None, "integrity": integrity, "rehearsal": rehearsal})
    _emit(payload)


def _sqlite_backup(database: Path, backup_dir: Path | None) -> Path:
    target_dir = backup_dir or database.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = target_dir / f"{database.name}.backup-{stamp}"
    source = sqlite3.connect(str(database))
    target = sqlite3.connect(str(backup))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return backup


def _integrity_check(database: Path) -> str:
    connection = sqlite3.connect(str(database))
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def _rehearse_message_units(database: Path, fts: Path, vectors: Path, scope: Scope) -> dict[str, Any]:
    from severian.application.message_units import migrate_message_units

    with tempfile.TemporaryDirectory(prefix="severian-s1-rehearsal-") as raw:
        work = Path(raw)
        for source, suffix in ((database, "db"), (fts, "fts"), (vectors, "vec")):
            if source.exists():
                shutil.copy2(source, work / f"rehearsal.{suffix}")
        bundle = build_default_service(
            database=work / "rehearsal.db",
            fts=work / "rehearsal.fts",
            vectors=work / "rehearsal.vec",
        )
        try:
            report = migrate_message_units(bundle.repository, scope, confirm=True, rebuild=bundle.projection)
        finally:
            bundle.close()
        return report.as_dict()


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
    view: str = typer.Option("now", "--view", help="now (current facts), history (with replaced ones) or changes."),
    as_of: str = typer.Option("", "--as-of", help="Answer as the store stood on this date (YYYY-MM-DD) or time."),
) -> None:
    """Search the scope and emit JSON hits."""
    if view not in SEARCH_VIEWS:
        raise typer.BadParameter(f"must be one of {', '.join(SEARCH_VIEWS)}", param_hint="--view")
    as_of_moment = parse_time(as_of) if as_of else None
    if as_of and as_of_moment is None:
        raise typer.BadParameter("must be a date (YYYY-MM-DD) or an ISO time", param_hint="--as-of")
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
                view=view,
                as_of=(as_of_moment.date() if len(as_of) <= 10 else as_of_moment) if as_of_moment else None,
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
                        **({"truth_state": hit.truth_state, "previous": list(hit.previous)} if view != "now" else {}),
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
    dream_tenant: str = typer.Option("", "--dream-tenant", help="With --dream-profile: run the dreaming tick."),
    dream_profile: str = typer.Option("", "--dream-profile", help="With --dream-tenant: run the dreaming tick."),
) -> None:
    """Process pending derive jobs, then (SEVERIAN_USER_FACTS on) pending fact jobs, then exit.

    Drains the durable derive job queue: claims pending jobs, derives
    observations into memories via the canonical derivation service,
    marks jobs done (or failed), and rebuilds projections for the
    touched scopes. With the switch on it then runs the background fact
    pass over queued ``extract_facts`` jobs (the same ``--max-jobs`` cap
    applies to it separately). With the switch off, fact jobs are left
    queued and the fact model is never called. Emits a JSON summary.
    """
    if bool(dream_tenant) != bool(dream_profile):
        raise typer.BadParameter("--dream-tenant and --dream-profile go together: the tick is per profile")
    if dream_tenant:
        _require_dreaming()
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
        facts_processed = facts_failed = facts_projection_failed = facts_gave_up = facts_deferred = 0
        facts_outage: dict[str, Any] = {}  # 0.6.3 keys, only when the fact pass runs (switch-off output unchanged)
        if bundle.derivation.fact_extractor is not None:  # wired only while SEVERIAN_USER_FACTS is on
            batch = 8 if max_jobs == 0 else min(8, max_jobs)
            facts = worker.extract_facts(
                worker_id=f"{worker_id}-facts",
                batch_size=batch,
                max_batches=None if max_jobs == 0 else max_jobs // batch,
            )
            facts_processed, facts_failed = facts.processed, facts.failed
            facts_projection_failed = facts.projection_failed
            facts_gave_up, facts_deferred = facts.gave_up, facts.deferred
            facts_outage = {
                "facts_dead_lettered": facts.dead_lettered,  # still unserved after every try: see dead-letter
                "facts_endpoint": facts.endpoint,  # down = nothing sent, no attempt spent; jobs wait
            }
        dream_payload: dict[str, Any] = {"dream_scopes": 0, "dream_jobs": [], "dream_processed": 0, "dream_failed": 0}
        if dream_tenant:
            # The opt-in dreaming tick: queue one job per (active scope, mode), then run them. No cron
            # is created or enabled anywhere; this only happens while the operator asks for it.
            queued, dream_summary = worker.dream_tick(
                tenant_id=dream_tenant,
                profile_id=dream_profile,
                writer=DreamWriter.from_env(),
                max_jobs=None if max_jobs == 0 else max_jobs,
            )
            dream_payload = {
                "dream_scopes": queued.scopes,
                "dream_modes": list(queued.modes),
                "dream_jobs": list(queued.jobs),
                "dream_processed": dream_summary.processed,
                "dream_failed": dream_summary.failed,
                "dream_projection_failed": dream_summary.projection_failed,
            }
        _emit(
            {
                "processed": summary.processed,
                "failed": summary.failed,
                "projection_failed": summary.projection_failed,
                "touched_scopes": [scope.model_dump() for scope in summary.touched_scopes],
                "facts_processed": facts_processed,
                "facts_failed": facts_failed,
                "facts_projection_failed": facts_projection_failed,
                "facts_gave_up": facts_gave_up,  # the model could not serve them: failed with the reason
                "facts_deferred": facts_deferred,  # the model was busy or away: waiting to be tried again
                **facts_outage,
                **dream_payload,
            }
        )
    finally:
        bundle.close()


def _require_dreaming() -> None:
    """Every Should behaviour of S2 sits behind one switch, off by default."""
    if not dreaming_enabled():
        raise typer.BadParameter(
            "the dreaming pass is off: set SEVERIAN_DREAMING=1 to enable the operator command, "
            "the worker tick and the merge-summary writer"
        )


@cli.command("dream")
def dream(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    collection: str = typer.Option(..., "--collection"),
    agent: str = typer.Option(..., "--agent"),
    session: str = typer.Option(..., "--session"),
    mode: str = typer.Option(
        ..., "--mode", help="consolidate | curate | retention | retention-archive | contradictions"
    ),
    policy: str = typer.Option("", "--policy", help="JSON retention policy (retention modes only)."),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Carry the pass out. Without it this is a dry run: the plan is printed and nothing is written.",
    ),
    dsn: str | None = typer.Option(None, "--dsn"),
) -> None:
    """Run ONE dreaming pass over a scope, dry run by default.

    The pass is deterministic: code decides what is a duplicate, a restatement or a contradiction
    (``dreamtriage``). With ``--confirm`` a consolidation supersedes the older duplicate heads and
    writes one summary per merged group, curation writes its review marker and report, retention
    writes its plan and archives only if asked. Without it the same plan is printed and nothing is
    written, and no model is called. The command refuses to run while ``SEVERIAN_DREAMING`` is off.
    """
    _require_dreaming()
    if mode not in DREAM_MODES:
        raise typer.BadParameter(f"--mode must be one of: {', '.join(DREAM_MODES)}")
    try:
        parsed_policy: dict[str, Any] = json.loads(policy) if policy.strip() else {}
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--policy is not JSON: {exc}") from exc
    if parsed_policy and not isinstance(parsed_policy, dict):
        raise typer.BadParameter("--policy must be a JSON object")
    bundle = build_default_service(database=database, fts=fts, vectors=vectors, dsn=dsn)
    try:
        scope = _scope_from_flags(tenant=tenant, profile=profile, collection=collection, agent=agent, session=session)
        payload: dict[str, Any] = {"mode": mode, "confirmed": bool(confirm)}
        if mode == "consolidate":
            # A dry run never calls a model: the plan shows the deterministic summary it would store.
            writer = DreamWriter.from_env() if confirm else None
            report = ConsolidationService(bundle.repository, writer=writer).consolidate(
                scope, exclude_kinds=MARKER_KINDS, dry_run=not confirm
            )
            payload.update(report.summary())
            payload["planned"] = report.planned
        elif mode == "curate":
            curation = CurationService(bundle.repository).curate(
                scope, exclude_kinds=MARKER_KINDS, dry_run=not confirm
            )
            payload.update(curation.summary())
            payload["low_value_ids"] = list(curation.low_value_ids)
        elif mode == "retention":
            evaluation = RetentionPolicyService(bundle.repository).evaluate(
                scope, parsed_policy, exclude_kinds=MARKER_KINDS
            )
            payload.update(evaluation.summary())
            payload["archived_candidates"] = list(evaluation.archived_ids)
        elif mode == "retention-archive":
            archived = RetentionPolicyService(bundle.repository).apply_archive(
                scope, parsed_policy, exclude_kinds=MARKER_KINDS, dry_run=not confirm
            )
            payload.update(archived.summary())
            payload["archived_candidates"] = list(archived.archived_ids)
        else:  # contradictions
            pairs = ContradictionScanService(bundle.repository).scan(scope, exclude_kinds=MARKER_KINDS)
            payload.update(
                {
                    "scope": scope.model_dump(),
                    "contradiction_pairs": len(pairs),
                    "pairs": [[pair.memory_a_id, pair.memory_b_id, round(pair.overlap, 4)] for pair in pairs],
                }
            )
        _emit(payload)
    except typer.BadParameter:
        raise
    except Exception as exc:
        raise typer.BadParameter(f"cannot dream: {exc}") from exc
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


@jobs_app.command("requeue")
def jobs_requeue(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    kind: str = typer.Option(..., "--kind", help="Job kind, e.g. extract_facts."),
    status: str = typer.Option("failed", "--status", help="The state to requeue from: failed or dead_letter."),
    transient: bool = typer.Option(
        False,
        "--transient",
        help="Only jobs whose error says the model server was down, busy or slow (never an unreadable reply).",
    ),
    confirm: bool = typer.Option(
        False, "--confirm", help="Requeue them. Without it this is a dry run: the plan is printed, nothing changes."
    ),
    dsn: str | None = typer.Option(None, "--dsn"),
) -> None:
    """Put failed or dead-lettered jobs of one kind back in the queue (0.6.3), dry run by default.

    Each requeued job returns to ``pending`` with a fresh attempt budget and gets one ``job.requeue``
    audit row (kind, state, attempts; never the error text). The next ``work`` pass retries them.
    """
    from severian.application.jobrecovery import REQUEUEABLE_JOB_STATUSES, plan_requeue, requeue

    if status not in REQUEUEABLE_JOB_STATUSES:
        raise typer.BadParameter("--status must be failed or dead_letter")
    bundle = build_default_service(database=database, fts=fts, vectors=vectors, dsn=dsn)
    try:
        plan = plan_requeue(bundle.repository, kind=kind, status=status, transient_only=transient)
        done = requeue(bundle.repository, plan) if confirm else []
        _emit(
            {
                "action": "requeue",
                "confirmed": bool(confirm),
                "kind": kind,
                "status": status,
                "error_class": "transient" if transient else "any",
                "matched": plan.matched,
                "skipped_other_errors": plan.skipped_other_errors,
                "requeued": len(done),
                "job_ids": list(plan.job_ids),
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


@cli.command("purge")
def purge(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    collection: str = typer.Option(..., "--collection"),
    agent: str = typer.Option(..., "--agent"),
    session: str = typer.Option(..., "--session"),
    observation: str = typer.Option(..., "--observation", help="Observation id whose whole revision chain to purge."),
    confirm: bool = typer.Option(
        False,
        "--confirm",
        help="Carry the purge out. Without it this is a dry run: the plan is printed and nothing is written.",
    ),
    dsn: str | None = typer.Option(None, "--dsn"),
) -> None:
    """Remove an observation's text from this store, for good (M1).

    Dry run by default: it lists every record id it would scrub, by type, and never prints
    content. ``--confirm`` scrubs the observation's whole revision chain, every memory
    derived from it (revisions and tombstones included), the relations that lose their last
    support and the entity names nothing else supports, then rebuilds the projection and
    compacts the SQLite stores (checkpoint + VACUUM).

    This is not a claim of physical erasure everywhere: backups and snapshots outside the
    store cannot be reached from here, and on PostgreSQL the old row versions stay on disk
    as dead tuples until an operator runs VACUUM. See ``docs/operator-runbook.md``.
    """
    bundle = build_default_service(database=database, fts=fts, vectors=vectors, dsn=dsn)
    try:
        scope = _scope_from_flags(tenant=tenant, profile=profile, collection=collection, agent=agent, session=session)
        plan = bundle.service.preview_purge(scope, observation)
        payload: dict[str, Any] = {
            "confirmed": False,
            "plan": plan.to_dict(),
            "applied": None,
            "warnings": _purge_warnings(bundle.backend),
            "operator_commands": _purge_operator_commands(bundle.backend),
            "compacted": [],
            "compaction_complete": False,
        }
        if not confirm:
            _emit(payload)
            return
        try:
            report = bundle.service.purge_observation(scope, observation)
        except Exception as exc:
            # Nothing was committed: this is a failed purge, not a partial one.
            # M1 R3: the reason only - the handler below adds "cannot purge: " once,
            # so the operator never reads the prefix twice.
            raise typer.BadParameter(str(exc)) from exc
        # The purge is committed. Everything below is physical cleanup, which can fail on
        # its own and must be reported, never asserted (M1 F3): a busy or failing store is a
        # warning after a committed purge, not a failed command.
        try:
            compacted = [result.__dict__ for result in bundle.compact()]
            compaction_error: str | None = None
        except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
            compacted = []
            compaction_error = f"{type(exc).__name__}: {exc}"
        complete = compaction_error is None and all(entry.get("checkpointed") and entry.get("vacuumed")
                                                     and entry.get("final_checkpointed")
                                                     for entry in compacted) and bool(compacted)
        payload.update(
            {
                "confirmed": True,
                "applied": {
                    "observations": report.observations,
                    "memories": report.memories,
                    "relations": report.relations,
                    "relations_retracted": report.relations_retracted,
                    "entities": report.entities,
                    "audit_rows": report.audit_rows,
                    "cancelled_jobs": report.cancelled_jobs,
                },
                "compacted": compacted,
                "compaction_complete": complete,
                "warnings": [
                    *_purge_warnings(bundle.backend),
                    *_compaction_warnings(compacted, error=compaction_error, backend=bundle.backend),
                ],
            }
        )
        _emit(payload)
    except Exception as exc:
        raise typer.BadParameter(f"cannot purge: {exc}") from exc
    finally:
        bundle.close()


def _purge_warnings(backend: str) -> list[str]:
    """The limits a purge must state, per backend. Never a "purged everywhere" claim.

    These are true before AND after the command runs, so they belong to the dry run too.
    What the compaction actually did is not one of them: see :func:`_compaction_warnings`,
    which speaks only from the per-store results (M1 F3).
    """
    common = [
        "Purge removes the text from this store only. Copies kept outside it cannot be "
        "reached: backups (for example ~/.hermes/backups/severian-*, ~/backups/severian-*) "
        "and snapshot files stay as they are.",
        "Deleting or rewriting those copies is an operator decision; this command never "
        "touches them.",
    ]
    if backend != "sqlite":
        return [
            *common,
            "PostgreSQL keeps the old row versions on disk as dead tuples until they are "
            "vacuumed. An ordinary VACUUM rewrites nothing in place; only VACUUM FULL "
            "rewrites the heap, and even then the WAL archives may still hold old images.",
        ]
    return common


def _compaction_warnings(
    compacted: list[dict[str, Any]], *, error: str | None, backend: str
) -> list[str]:
    """One truthful line per store whose physical cleanup did not finish, with the retry.

    Built from the ``CompactionResult`` fields, never from hope: a busy checkpoint, a
    failed VACUUM or a failing checkpoint after it is named with its file and its step.
    "The purge itself is committed" is stated separately, because those two outcomes are
    different things and an operator has to be able to tell them apart (M1 F3).
    """
    if backend != "sqlite":
        return []
    if error is not None:
        return [
            "The purge is committed: the text is gone from the canonical store. Physical "
            f"cleanup of the store files did NOT run ({error}).",
            "Re-run the compaction when the stores are idle (or delete and re-copy the store "
            "files from a compacted backup) before claiming the freed pages are gone.",
        ]
    incomplete = [entry for entry in compacted if not (
        entry.get("checkpointed") and entry.get("vacuumed") and entry.get("final_checkpointed")
    )]
    if not incomplete:
        if compacted:
            return [
                "The canonical, FTS and vector SQLite files were checkpointed and VACUUMed "
                "after the purge; their -wal/-shm sidecars are truncated with them."
            ]
        return ["No SQLite store file was compacted, so the freed pages stay in the files."]
    lines = [
        "The purge is committed: the text is gone from the canonical store, the export and "
        "the histories. Physical cleanup is INCOMPLETE for "
        f"{len(incomplete)} of {len(compacted)} store files."
    ]
    for entry in incomplete:
        steps = []
        if not entry.get("checkpointed"):
            steps.append("checkpoint")
        if not entry.get("vacuumed"):
            steps.append("VACUUM")
        if entry.get("final_checkpointed") is not True:
            steps.append("checkpoint-after-VACUUM")
        reason = entry.get("error") or "did not complete"
        lines.append(
            f"{entry.get('path')}: {', '.join(steps)} did not complete ({reason}). Retry when "
            "the store is idle (stop the worker and any open reader), then re-run the "
            "compaction."
        )
    return lines


def _purge_operator_commands(backend: str) -> list[str]:
    """Commands the operator may run afterwards. Printed, never executed by the CLI."""
    if backend == "sqlite":
        return []
    return [
        "psql \"$SEVERIAN_PG_DSN\" -c 'VACUUM (FULL, ANALYZE) records;'",
        "# then, at the filesystem level, the operator's own WAL-archive retention policy",
    ]


@cli.command("facts-rollback")
def facts_rollback(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    tenant: str = typer.Option(..., "--tenant"),
    profile: str = typer.Option(..., "--profile"),
    dsn: str | None = typer.Option(None, "--dsn"),
) -> None:
    """Erase every fact SEVERIAN_USER_FACTS made for one profile; raw turns and other memories stay.

    Run with the switch OFF and the worker stopped. Pending fact jobs are cancelled so nothing
    re-creates the facts. Erased facts leave reads and search but stay in history for audit.
    Safe to repeat.
    """
    bundle = build_default_service(database=database, fts=fts, vectors=vectors, dsn=dsn)
    try:
        done = bundle.service.rollback_user_facts(tenant, profile)
        _emit(
            {
                "status": "rolled_back",
                "tenant": tenant,
                "profile": profile,
                "scopes": done.scopes,
                "facts_erased": done.erased,
                "fact_jobs_cancelled": done.cancelled_jobs,
            }
        )
    except Exception as exc:
        raise typer.BadParameter(f"cannot roll back user facts: {exc}") from exc
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


@cli.command("audit")
def audit(
    database: Path = typer.Option(..., "--database"),
    fts: Path = typer.Option(..., "--fts"),
    vectors: Path = typer.Option(..., "--vectors"),
    repair: bool = typer.Option(
        False,
        "--repair",
        help="Fix only what is safe: re-queue stuck jobs, write missing lexical/vector/cue rows. Deletes nothing.",
    ),
) -> None:
    """Check the store and its indexes. Read-only unless --repair is given.

    Reports index counts against the store, missing or orphaned index rows, memories and relations
    that lost their source, stuck or dead-lettered jobs, and broken replacement chains. The report
    is read through read-only connections, so a plain run changes nothing. Exit code 1 when
    anything is left to fix (after the repair, if one was run).
    """
    from severian.infrastructure.audit import audit_store, repair_store

    try:
        before = audit_store(database, fts, vectors)
    except FileNotFoundError as error:
        raise typer.BadParameter(f"store file not found: {error}") from error
    payload: dict[str, Any] = {"format": "severian-audit-v1", "mode": "repair" if repair else "read-only"}
    final = before
    if repair:
        bundle = build_default_service(database=database, fts=fts, vectors=vectors)
        try:
            payload["repair"] = repair_store(bundle, before)
        finally:
            bundle.close()
        final = audit_store(database, fts, vectors)
        payload["before"] = before.to_dict()["findings_by_kind"]
    payload.update(final.to_dict())
    _emit(payload)
    if not final.ok:
        raise typer.Exit(code=1)


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
