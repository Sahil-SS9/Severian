"""Read-only store check and safe repair (Step 5, next-improvements plan).

``audit_store`` opens the canonical store, the lexical (FTS) index and the vector index through
SQLite read-only connections (``mode=ro``), so it cannot change a byte of any of them: no schema
creation, no WAL checkpoint, no repair. It reports:

* index counts against the store (lexical, vector, cue) and the rows that are missing or orphaned;
* active memories whose source observations are gone or superseded, and relations whose lineage
  or entities are gone;
* stuck jobs (a ``running`` lease that has expired) and dead-lettered jobs, and the pending outbox;
* truth-state consistency: replacement chains that point nowhere, loop, or lose their revision parent.

``repair_store`` fixes only what is safe, through the normal service wiring: it re-queues stuck jobs
and writes the lexical, vector and cue rows that are missing from their indexes. It never deletes a
memory, an observation or a relation, and it leaves orphaned index rows alone (they are reported;
``severian rebuild-vectors`` is the tool for those).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from severian.domain.models import ERASED, Scope

__all__ = ["AuditFinding", "AuditReport", "audit_store", "repair_store"]

_SCOPE_COLUMNS = ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")
_CHAIN_LIMIT = 10_000

ScopeKey = tuple[str, str, str, str, str]


@dataclass(frozen=True)
class AuditFinding:
    """One thing the audit found. ``repairable`` says whether ``--repair`` can fix it safely."""

    kind: str
    scope: str
    detail: str
    ids: tuple[str, ...] = ()
    repairable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scope": self.scope,
            "detail": self.detail,
            "ids": list(self.ids),
            "repairable": self.repairable,
        }


@dataclass
class AuditReport:
    counts: dict[str, dict[str, int]] = field(default_factory=dict)
    jobs: dict[str, int] = field(default_factory=dict)
    findings: list[AuditFinding] = field(default_factory=list)
    scope_keys: dict[str, ScopeKey] = field(default_factory=dict)

    def label(self, key: ScopeKey) -> str:
        """The readable name of a scope; remembers the real values so repair never has to split a string."""
        label = "/".join(key)
        self.scope_keys[label] = key
        return label

    @property
    def ok(self) -> bool:
        return not self.findings

    def to_dict(self) -> dict[str, Any]:
        by_kind: dict[str, int] = defaultdict(int)
        for finding in self.findings:
            by_kind[finding.kind] += 1
        return {
            "ok": self.ok,
            "finding_count": len(self.findings),
            "findings_by_kind": dict(sorted(by_kind.items())),
            "repairable_count": sum(1 for f in self.findings if f.repairable),
            "scopes": self.counts,
            "jobs": self.jobs,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@contextmanager
def _read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only SQLite connection: writes (including schema creation) raise instead of happening."""
    if not path.exists():
        raise FileNotFoundError(path)
    uri = f"file:{path.resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        connection.execute("PRAGMA query_only=ON")
        yield connection


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return row is not None


def _ids_by_scope(connection: sqlite3.Connection, table: str, id_column: str) -> dict[ScopeKey, set[str]]:
    result: dict[ScopeKey, set[str]] = defaultdict(set)
    columns = ", ".join(_SCOPE_COLUMNS)
    for row in connection.execute(f"SELECT {id_column}, {columns} FROM {table}"):  # noqa: S608 - fixed names
        result[(row[1], row[2], row[3], row[4], row[5])].add(row[0])
    return result


def audit_store(database: Path, fts: Path, vectors: Path, *, now: datetime | None = None) -> AuditReport:
    """Check the three stores without changing them."""
    moment = (now or datetime.now(UTC)).astimezone(UTC).isoformat()
    report = AuditReport()

    with _read_only(database) as store:
        memories: dict[str, dict[str, Any]] = {}
        memory_scope: dict[str, ScopeKey] = {}
        observations: dict[str, str | None] = {}
        relations: list[tuple[ScopeKey, dict[str, Any]]] = []
        entities: set[str] = set()
        rows = store.execute(
            "SELECT id, record_type, tenant_id, profile_id, collection_id, agent_id, session_id, status, "
            "lease_until, payload FROM records WHERE record_type IN "
            "('memory','observation','relation','entity','job','outbox')"
        )
        stuck: list[str] = []
        dead: list[str] = []
        job_counts: dict[str, int] = defaultdict(int)
        pending_outbox = 0
        for rid, kind, *scope_values, status, lease_until, raw in rows:
            key: ScopeKey = (scope_values[0], scope_values[1], scope_values[2], scope_values[3], scope_values[4])
            payload = json.loads(raw)
            if kind == "memory":
                memories[rid] = payload
                memory_scope[rid] = key
            elif kind == "observation":
                observations[rid] = payload.get("superseded_by_id")
            elif kind == "relation":
                relations.append((key, payload))
            elif kind == "entity":
                entities.add(rid)
            elif kind == "job":
                job_counts[str(status)] += 1
                if status == "running" and lease_until and lease_until < moment:
                    stuck.append(rid)
                if status == "dead_letter" or payload.get("dead_letter"):
                    dead.append(rid)
            elif kind == "outbox" and status == "pending":
                pending_outbox += 1
        report.jobs = {**dict(sorted(job_counts.items())), "stuck_leases": len(stuck), "pending_outbox": pending_outbox}
        if stuck:
            report.findings.append(
                AuditFinding("stuck_job", "-", f"{len(stuck)} running job(s) with an expired lease", tuple(stuck), True)
            )
        if dead:
            report.findings.append(
                AuditFinding(
                    "dead_letter_job",
                    "-",
                    f"{len(dead)} dead-lettered job(s); inspect with `severian dead-letter`",
                    tuple(dead),
                )
            )

    active: dict[ScopeKey, dict[str, dict[str, Any]]] = defaultdict(dict)
    for memory_id, payload in memories.items():
        if payload.get("superseded_by_id") is None:
            active[memory_scope[memory_id]][memory_id] = payload

    _check_sources(report, memories, memory_scope, observations, relations, entities)
    _check_truth_chains(report, memories, memory_scope)

    lexical_ids: dict[ScopeKey, set[str]] = {}
    cue_ids: dict[ScopeKey, set[str]] = {}
    has_cue_table = False
    with _read_only(fts) as index:
        if _has_table(index, "memory_fts"):
            lexical_ids = _ids_by_scope(index, "memory_fts", "memory_id")
        else:
            report.findings.append(AuditFinding("missing_index", "-", "lexical index has no memory_fts table"))
        has_cue_table = _has_table(index, "cue_fts")
        if has_cue_table:
            cue_ids = _ids_by_scope(index, "cue_fts", "memory_id")
    vector_ids: dict[ScopeKey, set[str]] = {}
    with _read_only(vectors) as vector_store:
        if _has_table(vector_store, "memory_vectors"):
            vector_ids = _ids_by_scope(vector_store, "memory_vectors", "memory_id")
        else:
            report.findings.append(AuditFinding("missing_index", "-", "vector index has no memory_vectors table"))

    for key in sorted(set(active) | set(lexical_ids) | set(vector_ids) | set(cue_ids)):
        store_ids = set(active.get(key, {}))
        cued = {mid for mid, payload in active.get(key, {}).items() if payload.get("cues")}
        report.counts[report.label(key)] = {
            "memories": len(store_ids),
            "lexical": len(lexical_ids.get(key, set())),
            "vector": len(vector_ids.get(key, set())),
            "cues_expected": len(cued),
            "cues_indexed": len(cue_ids.get(key, set())),
        }
        label = report.label(key)
        _compare(report, label, "lexical", store_ids, lexical_ids.get(key, set()))
        _compare(report, label, "vector", store_ids, vector_ids.get(key, set()))
        missing_cues = cued - cue_ids.get(key, set())
        if missing_cues:
            report.findings.append(
                AuditFinding(
                    "missing_cue_row",
                    label,
                    f"{len(missing_cues)} memory(ies) with cues have no cue row",
                    tuple(sorted(missing_cues)),
                    True,
                )
            )
        orphan_cues = cue_ids.get(key, set()) - store_ids
        if orphan_cues:
            report.findings.append(
                AuditFinding(
                    "orphan_cue_row",
                    label,
                    f"{len(orphan_cues)} cue row(s) belong to no active memory",
                    tuple(sorted(orphan_cues)),
                )
            )
    return report


def _compare(report: AuditReport, label: str, index: str, store_ids: set[str], index_ids: set[str]) -> None:
    missing = store_ids - index_ids
    orphans = index_ids - store_ids
    if missing:
        report.findings.append(
            AuditFinding(
                f"missing_{index}_row",
                label,
                f"{len(missing)} active memory(ies) have no {index} row",
                tuple(sorted(missing)),
                True,
            )
        )
    if orphans:
        report.findings.append(
            AuditFinding(
                f"orphan_{index}_row",
                label,
                f"{len(orphans)} {index} row(s) belong to no active memory",
                tuple(sorted(orphans)),
            )
        )


def _check_sources(
    report: AuditReport,
    memories: dict[str, dict[str, Any]],
    memory_scope: dict[str, ScopeKey],
    observations: dict[str, str | None],
    relations: list[tuple[ScopeKey, dict[str, Any]]],
    entities: set[str],
) -> None:
    def alive(observation_id: str) -> bool:
        return observation_id in observations and observations[observation_id] is None

    bad_memories: dict[ScopeKey, list[str]] = defaultdict(list)
    for memory_id, payload in memories.items():
        if payload.get("superseded_by_id") is not None:
            continue
        sources = payload.get("source_observation_ids") or []
        if sources and not any(alive(source) for source in sources):
            bad_memories[memory_scope[memory_id]].append(memory_id)
    for key, ids in sorted(bad_memories.items()):
        report.findings.append(
            AuditFinding(
                "memory_without_source",
                report.label(key),
                f"{len(ids)} active memory(ies) whose source observations are gone or superseded",
                tuple(sorted(ids)),
            )
        )

    unsourced: dict[ScopeKey, list[str]] = defaultdict(list)
    dangling: dict[ScopeKey, list[str]] = defaultdict(list)
    for key, payload in relations:
        if payload.get("retracted_at"):
            continue
        relation_id = str(payload.get("id", ""))
        sources = payload.get("source_observation_ids") or []
        # Manually added relations have no lineage on purpose; only derived ones can lose their source.
        if sources and not any(alive(source) for source in sources):
            unsourced[key].append(relation_id)
        if payload.get("subject_id") not in entities or payload.get("object_id") not in entities:
            dangling[key].append(relation_id)
    for key, ids in sorted(unsourced.items()):
        report.findings.append(
            AuditFinding(
                "relation_without_source",
                report.label(key),
                f"{len(ids)} current relation(s) whose source observations are gone or superseded",
                tuple(sorted(ids)),
            )
        )
    for key, ids in sorted(dangling.items()):
        report.findings.append(
            AuditFinding(
                "relation_missing_entity",
                report.label(key),
                f"{len(ids)} relation(s) point at an entity that does not exist",
                tuple(sorted(ids)),
            )
        )


def _check_truth_chains(
    report: AuditReport, memories: dict[str, dict[str, Any]], memory_scope: dict[str, ScopeKey]
) -> None:
    """Truth state is computed at read time from replacement links, so those links must be sound."""
    broken: dict[ScopeKey, list[str]] = defaultdict(list)
    looping: dict[ScopeKey, list[str]] = defaultdict(list)
    orphan_revisions: dict[ScopeKey, list[str]] = defaultdict(list)
    for memory_id, payload in memories.items():
        key = memory_scope[memory_id]
        target = payload.get("superseded_by_id")
        if target is not None and target != ERASED and target not in memories:
            broken[key].append(memory_id)
        parent = payload.get("revision_of_id")
        if parent is not None and parent not in memories:
            orphan_revisions[key].append(memory_id)
        seen = {memory_id}
        current = target
        steps = 0
        while current is not None and current != ERASED and current in memories and steps < _CHAIN_LIMIT:
            if current in seen:
                looping[key].append(memory_id)
                break
            seen.add(current)
            current = memories[current].get("superseded_by_id")
            steps += 1
    for label, groups, text in (
        ("replacement_target_missing", broken, "memory(ies) say they were replaced by a memory that does not exist"),
        ("replacement_loop", looping, "memory(ies) sit in a replacement loop"),
        ("revision_parent_missing", orphan_revisions, "revision(s) whose parent memory does not exist"),
    ):
        for key, ids in sorted(groups.items()):
            report.findings.append(AuditFinding(label, report.label(key), f"{len(ids)} {text}", tuple(sorted(ids))))


# -- repair ---------------------------------------------------------------------------------


def repair_store(bundle: Any, report: AuditReport) -> dict[str, Any]:
    """Fix the repairable findings in ``report`` using an opened service bundle.

    Safe fixes only: re-queue stuck jobs and write missing lexical / vector / cue rows. Nothing is deleted.
    Each fix is tried on its own so one failure (for example a vector embedding that does not match
    the index) is reported and does not stop the others.
    """
    outcome: dict[str, Any] = {"requeued_jobs": 0, "lexical_rows": 0, "vector_rows": 0, "cue_rows": 0, "errors": []}
    if any(f.kind == "stuck_job" for f in report.findings):
        outcome["requeued_jobs"] = _attempt(outcome, "stuck_job", lambda: bundle.repository.reap_expired_jobs())

    wanted: dict[str, dict[ScopeKey, set[str]]] = {
        "lexical": defaultdict(set),
        "vector": defaultdict(set),
        "cue": defaultdict(set),
    }
    for finding in report.findings:
        if not finding.repairable or finding.kind == "stuck_job":
            continue
        index = {"missing_lexical_row": "lexical", "missing_vector_row": "vector", "missing_cue_row": "cue"}[
            finding.kind
        ]
        wanted[index][report.scope_keys[finding.scope]].update(finding.ids)

    for index, by_scope in wanted.items():
        for key, ids in by_scope.items():
            scope = Scope(**dict(zip(_SCOPE_COLUMNS, key, strict=True)))
            memories = []
            for memory_id in sorted(ids):
                try:
                    memory = bundle.repository.get_memory(scope, memory_id)
                except Exception as error:  # noqa: BLE001 - report and continue
                    outcome["errors"].append(f"{index} {memory_id}: {error}")
                    continue
                if memory.superseded_by_id is None:
                    memories.append(memory)
            if not memories:
                continue
            target = bundle.vector if index == "vector" else bundle.lexical
            written = _attempt(outcome, index, partial(_write, target, memories))
            outcome[f"{index}_rows"] += written
    return outcome


def _write(index: Any, memories: list[Any]) -> int:
    index.upsert_many(memories)
    return len(memories)


def _attempt(outcome: dict[str, Any], label: str, action: Callable[[], int]) -> int:
    try:
        return int(action())
    except Exception as error:  # noqa: BLE001 - one failed fix must not stop the others
        outcome["errors"].append(f"{label}: {error}")
        return 0
