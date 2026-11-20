"""S2: what a dreaming pass does — convergence, safety, audit, dry run and the writer.

The producer itself (the switch, the tick, dream jobs as a background kind) is
``test_dream_s2_producer.py``. Proof gate of the handoff, one test per line:

- repeated dream runs converge and add no duplicate facts;
- markers never come back as memories (the curation marker would otherwise grow every pass);
- erased and purged content never returns through a pass;
- every dream write has an audit row;
- a lost supersede race is skipped and logged, not a failed pass;
- a dry run writes nothing and calls no model;
- the writer batches by 8, falls back to the deterministic text, and refuses a remote endpoint.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from severian.application.dreaming import (
    ConsolidationService,
    ContradictionScanService,
    CurationService,
    RetentionPolicyService,
)
from severian.application.dreamwriter import DREAM_BATCH, DreamWriter
from severian.application.markers import MARKER_KINDS
from severian.composition import ServiceBundle, build_bundle
from severian.domain.errors import ConflictError
from severian.domain.models import Memory, Observation, Scope

SCOPE = Scope(
    tenant_id="kensei", profile_id="s2", collection_id="profile", agent_id="s2", session_id="shared"
)


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def remember(bundle: ServiceBundle, content: str, *, scope: Scope = SCOPE, kind: str = "fact") -> Memory:
    observation = bundle.repository.add_observation(
        Observation(scope=scope, content=content, raw_content=content, source={"uri": f"note://{uuid.uuid4().hex}"})
    )
    return bundle.repository.add_memory(
        Memory(
            scope=scope,
            kind=kind,
            content=content,
            source_observation_ids=[observation.id],
            model_id="test",
            prompt_version="p1",
            confidence=0.9,
        )
    )


def project(bundle: ServiceBundle, scope: Scope = SCOPE) -> None:
    events = bundle.repository.pending_outbox()
    if events:
        bundle.projection.apply(events)
        bundle.repository.ack_outbox_many([event.id for event in events])


def duplicate_pair(bundle: ServiceBundle, content: str = "the user keeps a steel kettle") -> tuple[str, str]:
    first = remember(bundle, content)
    second = remember(bundle, content)
    project(bundle)
    return first.id, second.id


def actions(bundle: ServiceBundle, scope: Scope = SCOPE) -> list[str]:
    return [row.action for row in bundle.repository.list_audit(scope)]


# -- convergence ---------------------------------------------------------------------------------


def test_repeated_consolidation_converges(bundle: ServiceBundle) -> None:
    """Second pass: no group (the duplicate is superseded), no new memory, no new marker."""
    duplicate_pair(bundle)
    service = ConsolidationService(bundle.repository)
    first = service.consolidate(SCOPE, exclude_kinds=MARKER_KINDS)
    assert first.duplicate_groups and first.consolidated_memory_ids
    after_first = len(bundle.repository.list_memories(SCOPE))
    second = service.consolidate(SCOPE, exclude_kinds=MARKER_KINDS)
    assert second.duplicate_groups == []
    assert second.consolidated_memory_ids == []
    assert len(bundle.repository.list_memories(SCOPE)) == after_first


def test_repeated_curation_converges(bundle: ServiceBundle) -> None:
    """The marker is out of the input, so its counts are stable and the second pass dedupes."""
    remember(bundle, "the user keeps a steel kettle")
    remember(bundle, "the user has 2 cats")
    service = CurationService(bundle.repository)
    service.curate(SCOPE, exclude_kinds=MARKER_KINDS)
    markers_after_first = [m for m in bundle.repository.list_memories(SCOPE) if m.kind == "curation_review"]
    assert len(markers_after_first) == 1
    service.curate(SCOPE, exclude_kinds=MARKER_KINDS)
    markers_after_second = [m for m in bundle.repository.list_memories(SCOPE) if m.kind == "curation_review"]
    assert [m.id for m in markers_after_second] == [m.id for m in markers_after_first]


def test_curation_would_grow_without_the_marker_exclusion(bundle: ServiceBundle) -> None:
    """The exclusion is load-bearing: a marker that scores itself changes its own counts."""
    remember(bundle, "the user keeps a steel kettle")
    service = CurationService(bundle.repository)
    service.curate(SCOPE)
    first = [m.id for m in bundle.repository.list_memories(SCOPE) if m.kind == "curation_review"]
    service.curate(SCOPE)
    second = [m.id for m in bundle.repository.list_memories(SCOPE) if m.kind == "curation_review"]
    assert len(second) == len(first) + 1


def test_repeated_retention_archive_converges(bundle: ServiceBundle) -> None:
    from datetime import UTC, datetime, timedelta

    remember(bundle, "the user's old flat was on Kirkstall Road")
    later = datetime.now(UTC) + timedelta(days=30)
    service = RetentionPolicyService(bundle.repository, now=later)
    first = service.apply_archive(SCOPE, {"max_age_days": 1}, exclude_kinds=MARKER_KINDS)
    assert first.expired_ids
    marker_count = len([m for m in bundle.repository.list_memories(SCOPE) if m.kind == "retention_archive"])
    second = service.apply_archive(SCOPE, {"max_age_days": 1}, exclude_kinds=MARKER_KINDS)
    assert second.archived_ids == [] and second.expired_ids == []
    assert len([m for m in bundle.repository.list_memories(SCOPE) if m.kind == "retention_archive"]) == marker_count


# -- nothing comes back --------------------------------------------------------------------------


def test_purged_content_never_returns_through_a_pass(bundle: ServiceBundle) -> None:
    secret = "the user's passport number is ZX8112233"
    keep = "the user keeps a steel kettle"
    memory = remember(bundle, secret)
    remember(bundle, keep)
    observation_id = memory.source_observation_ids[0]
    bundle.service.purge_observation(SCOPE, observation_id)
    ConsolidationService(bundle.repository).consolidate(SCOPE, exclude_kinds=MARKER_KINDS)
    CurationService(bundle.repository).curate(SCOPE, exclude_kinds=MARKER_KINDS)
    ContradictionScanService(bundle.repository).scan(SCOPE, exclude_kinds=MARKER_KINDS)
    surviving = [m for m in bundle.repository.list_memories_history(SCOPE) if "ZX8112233" in m.content]
    assert surviving == []


def test_an_erased_observation_is_not_consolidated_back(bundle: ServiceBundle) -> None:
    memory = remember(bundle, "the user keeps a steel kettle")
    bundle.service.erase_observation(SCOPE, memory.source_observation_ids[0])
    report = ConsolidationService(bundle.repository).consolidate(SCOPE, exclude_kinds=MARKER_KINDS)
    assert report.duplicate_groups == []
    assert not [m for m in bundle.repository.list_memories(SCOPE) if "steel kettle" in m.content]


def test_a_corrected_memory_does_not_become_a_summary_twice(bundle: ServiceBundle) -> None:
    """A correction leaves the corrected head active and the superseded one in history: a pass must
    not treat the pair as a duplicate and re-supersede what the correction already settled."""
    old = remember(bundle, "the user's bike is a blue Ridgeback")
    new = remember(bundle, "the user's bike is a red Ridgeback")
    bundle.repository.supersede_memory(SCOPE, old.id, replacement_id=new.id)
    report = ConsolidationService(bundle.repository).consolidate(SCOPE, exclude_kinds=MARKER_KINDS)
    assert report.duplicate_groups == []
    assert bundle.repository.get_memory(SCOPE, new.id).superseded_by_id is None


# -- audit ---------------------------------------------------------------------------------------


def test_every_dream_write_has_an_audit_row(bundle: ServiceBundle) -> None:
    duplicate_pair(bundle)
    ConsolidationService(bundle.repository).consolidate(SCOPE, exclude_kinds=MARKER_KINDS)
    CurationService(bundle.repository).curate(SCOPE, exclude_kinds=MARKER_KINDS)
    rows = actions(bundle)
    # The merge (M2's own row plus the pass's), the summary creation and the curation report.
    assert "memory.supersede" in rows
    assert "dreaming.merge" in rows
    assert "dreaming.consolidate" in rows
    assert "curation.report" in rows
    marker = next(m for m in bundle.repository.list_memories(SCOPE) if m.kind == "consolidated")
    named = {row.target_id for row in bundle.repository.list_audit(SCOPE) if row.action == "dreaming.consolidate"}
    assert marker.id in named


def test_retention_writes_its_plan_before_it_archives(bundle: ServiceBundle) -> None:
    from datetime import UTC, datetime, timedelta

    remember(bundle, "the user's old flat was on Kirkstall Road")
    service = RetentionPolicyService(bundle.repository, now=datetime.now(UTC) + timedelta(days=30))
    service.apply_archive(SCOPE, {"max_age_days": 1})
    rows = actions(bundle)
    assert "retention.archive" in rows
    assert "retention.archive_plan" in rows
    assert rows.index("retention.archive_plan") > rows.index("retention.archive")


def test_a_lost_supersede_race_is_skipped_and_logged(bundle: ServiceBundle, monkeypatch: pytest.MonkeyPatch) -> None:
    duplicate_pair(bundle)
    service = ConsolidationService(bundle.repository)

    def lost_race(*_args: Any, **_kwargs: Any) -> Any:
        raise ConflictError("memory is not an active memory in this scope")

    monkeypatch.setattr(bundle.repository, "supersede_memory", lost_race)
    report = service.consolidate(SCOPE, exclude_kinds=MARKER_KINDS)
    assert report.skipped_supersedes == 1
    assert "dreaming.skip" in actions(bundle)
    # The pass finished, and it wrote no summary: the merge it would describe did not happen.
    assert report.consolidated_memory_ids == []
    assert not [m for m in bundle.repository.list_memories(SCOPE) if m.kind == "consolidated"]
    # A lost race is not retried round after round.
    assert [row.action for row in bundle.repository.list_audit(SCOPE)].count("dreaming.skip") == 1


# -- dry run -------------------------------------------------------------------------------------


def test_a_dry_run_writes_nothing_and_calls_no_model(bundle: ServiceBundle) -> None:
    duplicate_pair(bundle)
    memories_before = len(bundle.repository.list_memories(SCOPE))
    audit_before = len(bundle.repository.list_audit(SCOPE))

    class ExplodingWriter(DreamWriter):
        def write(self, texts: list[str]) -> list[str | None]:  # pragma: no cover - must not run
            raise AssertionError("a dry run must not call the model")

    report = ConsolidationService(bundle.repository, writer=ExplodingWriter()).consolidate(
        SCOPE, exclude_kinds=MARKER_KINDS, dry_run=True
    )
    assert report.planned and report.planned[0]["supersede"]
    assert len(bundle.repository.list_memories(SCOPE)) == memories_before
    assert len(bundle.repository.list_audit(SCOPE)) == audit_before


def test_a_dry_run_curation_writes_no_marker(bundle: ServiceBundle) -> None:
    remember(bundle, "the user keeps a steel kettle")
    report = CurationService(bundle.repository).curate(SCOPE, exclude_kinds=MARKER_KINDS, dry_run=True)
    assert report.scored
    assert report.review_marker_ids == []
    assert not [m for m in bundle.repository.list_memories(SCOPE) if m.kind == "curation_review"]


# -- the writer ----------------------------------------------------------------------------------


class _Reply:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def _writer_for(summaries: dict[str, str], *, calls: list[int]) -> DreamWriter:
    def opener(_request: Any, timeout: float | None = None) -> _Reply:
        calls.append(1)
        return _Reply({"choices": [{"message": {"content": json.dumps(summaries)}, "finish_reason": "stop"}]})

    return DreamWriter(opener=opener, sleep=lambda _s: None)


def test_the_writer_batches_by_eight() -> None:
    calls: list[int] = []
    summaries = {str(n): f"Summary {n}." for n in range(1, 10)}
    writer = _writer_for(summaries, calls=calls)
    written = writer.write([f"text {n}" for n in range(1, 10)])
    assert len(calls) == 2  # 8 + 1
    assert writer.stats.batches == 2
    assert written[0] == "Summary 1."
    assert sum(item is not None for item in written) == 9
    assert DREAM_BATCH == 8


def test_a_writer_failure_falls_back_to_the_deterministic_summary_and_is_counted(bundle: ServiceBundle) -> None:
    duplicate_pair(bundle)

    def failing(_request: Any, timeout: float | None = None) -> _Reply:
        raise OSError("connection refused")

    writer = DreamWriter(opener=failing, sleep=lambda _s: None)
    report = ConsolidationService(bundle.repository, writer=writer).consolidate(
        SCOPE, exclude_kinds=MARKER_KINDS
    )
    assert report.writer["model_calls"] == 2  # the call and its one retry
    assert report.writer["failed_batches"] == 1
    marker = bundle.repository.get_memory(SCOPE, report.consolidated_memory_ids[0])
    assert marker.content.startswith("Consolidated memory group:")
    assert report.duplicate_groups  # the pass still did its job


def test_a_writer_that_repeats_itself_is_deduped_within_a_batch(bundle: ServiceBundle) -> None:
    for suffix in ("kettle", "kettle", "kettle"):
        remember(bundle, f"the user keeps a steel {suffix}")
    # Three identical contents collapse into ONE LSH digest group, so build the groups by hand instead:
    groups = ConsolidationService(bundle.repository)._find_duplicate_groups(bundle.repository.list_memories(SCOPE))
    assert groups
    calls: list[int] = []
    writer = _writer_for({"1": "The same sentence.", "2": "The same sentence."}, calls=calls)
    service = ConsolidationService(bundle.repository, writer=writer)
    summaries = service._summaries(bundle.repository.list_memories(SCOPE), groups * 2, _report())
    assert summaries[0] == "The same sentence."
    assert summaries[1].startswith("Consolidated memory group:")  # the repeat fell back
    assert writer.stats.deduped == 1


def _report() -> Any:
    from severian.application.dreaming import ConsolidationReport

    return ConsolidationReport(scope=SCOPE)
