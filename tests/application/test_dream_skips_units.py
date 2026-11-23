"""0.6.5: dreaming never reads S1 message units.

A ``message_unit`` is a search-only copy of one message from a turn the store already holds. It is
not a separate memory of the user, so the dreaming pass has nothing to merge, score, archive or
contradict in it. On the live store the units are ~60% of all memories, and reading them made the
nightly pass run out of memory (2.8x more input). Every dream service must skip them on its own,
whatever ``exclude_kinds`` the caller passes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from severian.application.dreaming import (
    ConsolidationService,
    ContradictionScanService,
    CurationService,
    RetentionPolicyService,
)
from severian.application.message_units import MESSAGE_UNIT_KIND
from severian.composition import ServiceBundle, build_bundle
from severian.domain.models import Memory, Observation, Scope

SCOPE = Scope(tenant_id="kensei", profile_id="s065", collection_id="profile", agent_id="s065", session_id="shared")


@pytest.fixture
def bundle(tmp_path: Path):
    built = build_bundle(database=tmp_path / "s.db", fts=tmp_path / "s.fts", vectors=tmp_path / "s.vec")
    yield built
    built.close()


def remember(bundle: ServiceBundle, content: str, *, kind: str) -> str:
    observation = bundle.repository.add_observation(
        Observation(scope=SCOPE, content=content, raw_content=content, source={"uri": f"note://{uuid.uuid4().hex}"})
    )
    memory = bundle.repository.add_memory(
        Memory(
            scope=SCOPE,
            kind=kind,
            content=content,
            source_observation_ids=[observation.id],
            model_id="test",
            prompt_version="p1",
            confidence=0.9,
        )
    )
    return memory.id


def units(bundle: ServiceBundle) -> list[str]:
    """Two identical units plus two that contradict each other on one value."""
    return [
        remember(bundle, "the user's bike is a blue Ridgeback tourer", kind=MESSAGE_UNIT_KIND),
        remember(bundle, "the user's bike is a blue Ridgeback tourer", kind=MESSAGE_UNIT_KIND),
        remember(bundle, "the user's train leaves at 7 from platform 4", kind=MESSAGE_UNIT_KIND),
        remember(bundle, "the user's train leaves at 9 from platform 4", kind=MESSAGE_UNIT_KIND),
    ]


def active_ids(bundle: ServiceBundle) -> set[str]:
    return {memory.id for memory in bundle.repository.list_memories(SCOPE)}


def test_consolidate_never_merges_units(bundle: ServiceBundle) -> None:
    unit_ids = units(bundle)
    report = ConsolidationService(bundle.repository).consolidate(SCOPE)
    assert report.duplicate_groups == []
    assert report.consolidated_memory_ids == []
    assert set(unit_ids) <= active_ids(bundle), "a unit was superseded"


def test_consolidate_still_merges_ordinary_duplicates_next_to_units(bundle: ServiceBundle) -> None:
    units(bundle)
    remember(bundle, "the user keeps the bike in the shed", kind="fact")
    remember(bundle, "the user keeps the bike in the shed", kind="fact")
    report = ConsolidationService(bundle.repository).consolidate(SCOPE)
    assert len(report.duplicate_groups) == 1
    grouped = {report.duplicate_groups[0].survivor_id, *report.duplicate_groups[0].superseded_ids}
    kinds = {memory.kind for memory in bundle.repository.list_memories_history(SCOPE) if memory.id in grouped}
    assert kinds == {"fact"}


def test_curate_never_scores_units(bundle: ServiceBundle) -> None:
    unit_ids = set(units(bundle))
    fact_id = remember(bundle, "the user keeps the bike in the shed", kind="fact")
    report = CurationService(bundle.repository).curate(SCOPE, dry_run=True)
    scored = {row["memory_id"] if "memory_id" in row else row["id"] for row in report.scored}
    assert fact_id in scored
    assert not scored & unit_ids
    assert not set(report.low_value_ids) & unit_ids
    assert not {i for pair in report.contradiction_ids for i in pair} & unit_ids


def test_retention_never_touches_units(bundle: ServiceBundle) -> None:
    unit_ids = set(units(bundle))
    later = datetime.now(UTC) + timedelta(days=3650)
    result = RetentionPolicyService(bundle.repository, now=later).evaluate(SCOPE, {"max_age_days": 1}, dry_run=True)
    seen = {*result.archived_ids, *result.expired_ids, *result.kept_ids}
    assert not seen & unit_ids


def test_contradiction_scan_never_pairs_units(bundle: ServiceBundle) -> None:
    unit_ids = set(units(bundle))
    pairs = ContradictionScanService(bundle.repository).scan(SCOPE)
    assert not {i for p in pairs for i in (p.memory_a_id, p.memory_b_id)} & unit_ids
