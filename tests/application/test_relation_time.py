"""S3: relation validity from event time (``SEVERIAN_RELATION_EVENT_TIME``).

The derive path already knows when each source message was written
(``eventtime.message_time``); these tests pin that a relation uses that instant
as its start, that a replacement ends the old value at the NEWER statement's
instant, that mixed zones and naive save times are compared as UTC instants, and
that an earlier statement never ends a later fact. With the switch off, nothing
about the write path or the graph boundary changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from severian.application.derivation import DerivationService
from severian.application.extraction import EntityExtractionService
from severian.application.graph import GraphRetrievalService
from severian.domain.models import Collection, Observation, Scope
from severian.infrastructure.sqlite import SQLiteRepository

# A fixed save time, so "the wall clock" and "the event time" can never be confused.
SAVED_AT = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


class _Gateway:
    """One fact out per observation; the graph comes from rule extraction, not the model."""

    model_id = "local/hashing"
    prompt_version = "prompt-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]


def _repo(tmp_path: Path) -> SQLiteRepository:
    repo = SQLiteRepository(path=tmp_path / "sev.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    return repo


def _scope() -> Scope:
    return Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


def _observe(
    repo: SQLiteRepository, scope: Scope, content: str, *, when: str | datetime | None
) -> Observation:
    """One observation, saved at ``SAVED_AT``, whose source message carries ``when``."""
    source: dict[str, object] = {} if when is None else {"timestamp": when}
    return repo.add_observation(
        Observation(scope=scope, content=content, source=source, created_at=SAVED_AT)
    )


def _derive(repo: SQLiteRepository, scope: Scope, observation: Observation) -> None:
    """The production derive call, with the sibling switches pinned off so only S3 can act."""
    service = DerivationService(
        repo,
        extraction=EntityExtractionService(repo),
        event_time=False,
        date_stamps=False,
        anticipation=False,
    )
    service.derive_observation(scope, observation.id, _Gateway())


def _named(repo: SQLiteRepository, scope: Scope, object_name: str, predicate: str = "works_at"):
    """The single relation whose object carries ``object_name``."""
    names = {entity.id: entity.name.casefold() for entity in repo.list_entities(scope)}
    found = [
        relation
        for relation in repo.list_relations(scope)
        if relation.predicate == predicate and names.get(relation.object_id) == object_name.casefold()
    ]
    assert len(found) == 1, found
    return found[0]


def _visible_at(repo: SQLiteRepository, scope: Scope, entity_id: str, at: datetime) -> list[str]:
    """Ids of the relations graph retrieval treats as current at ``at``."""
    graph = GraphRetrievalService(repo, now=at)
    return [relation.id for relation in graph.neighborhood(scope, entity_id, depth=1).relations]


# -- start time --------------------------------------------------------------


def test_a_derived_relation_starts_at_its_source_messages_event_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    _derive(repo, scope, _observe(repo, scope, "Alice works at Acme", when="2026-03-01T10:00:00Z"))

    relation = _named(repo, scope, "Acme")
    assert relation.valid_from == datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert relation.valid_to is None


def test_a_naive_save_time_is_read_as_utc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No timestamp in the source: the save time is the event time, and a naive one means UTC."""
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    naive = datetime(2026, 3, 1, 10, 0)  # no tzinfo: naive local, taken as UTC
    observation = repo.add_observation(
        Observation(scope=scope, content="Alice works at Acme", created_at=naive)
    )
    _derive(repo, scope, observation)

    assert _named(repo, scope, "Acme").valid_from == datetime(2026, 3, 1, 10, tzinfo=UTC)


# -- end time ----------------------------------------------------------------


def test_a_replacement_ends_the_old_value_at_the_new_statements_event_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    _derive(repo, scope, _observe(repo, scope, "Alice works at Acme", when="2026-01-01T00:00:00Z"))
    _derive(repo, scope, _observe(repo, scope, "Alice works at Globex", when="2026-06-01T00:00:00Z"))

    acme = _named(repo, scope, "Acme")
    globex = _named(repo, scope, "Globex")
    assert acme.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert acme.valid_to == datetime(2026, 6, 1, tzinfo=UTC)  # not the wall clock
    assert globex.valid_from == datetime(2026, 6, 1, tzinfo=UTC)
    # [start, end): the handover instant belongs to the newer fact only.
    assert acme.valid_to == globex.valid_from


def test_mixed_time_zones_are_compared_as_utc_instants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two statements on the same calendar day in different zones still end in the right order."""
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    # 09:00+05:00 is 04:00Z; 06:00-02:00 is 08:00Z. Both are "1 January" locally.
    _derive(repo, scope, _observe(repo, scope, "Alice works at Acme", when="2026-01-01T09:00:00+05:00"))
    _derive(repo, scope, _observe(repo, scope, "Alice works at Globex", when="2026-01-01T06:00:00-02:00"))

    acme = _named(repo, scope, "Acme")
    globex = _named(repo, scope, "Globex")
    assert acme.valid_from == datetime(2026, 1, 1, 4, tzinfo=UTC)
    assert acme.valid_to == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert globex.valid_from == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert acme.valid_to is not None
    assert acme.valid_to >= datetime(2026, 1, 1, 4, tzinfo=UTC)  # a real window, never inverted


def test_an_earlier_statement_does_not_end_a_later_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out-of-order news: the older statement is history, not a replacement."""
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    _derive(repo, scope, _observe(repo, scope, "Alice works at Acme", when="2026-06-01T00:00:00Z"))
    _derive(repo, scope, _observe(repo, scope, "Alice works at Globex", when="2025-01-01T00:00:00Z"))

    assert _named(repo, scope, "Acme").valid_to is None  # still the current value
    assert _named(repo, scope, "Globex").valid_from == datetime(2025, 1, 1, tzinfo=UTC)


def test_restating_the_old_value_reopens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    _derive(repo, scope, _observe(repo, scope, "Alice works at Acme", when="2026-01-01T00:00:00Z"))
    _derive(repo, scope, _observe(repo, scope, "Alice works at Globex", when="2026-06-01T00:00:00Z"))
    _derive(repo, scope, _observe(repo, scope, "Alice works at Acme again", when="2026-09-01T00:00:00Z"))

    acme = _named(repo, scope, "Acme")
    assert acme.valid_to is None
    assert acme.valid_from == datetime(2026, 1, 1, tzinfo=UTC)


# -- both facts stay visible when both can be true ---------------------------


def test_compatible_facts_both_stay_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``works_with`` is not single-valued: two values never replace each other."""
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    _derive(repo, scope, _observe(repo, scope, "Alice works with Bob", when="2026-01-01T00:00:00Z"))
    _derive(repo, scope, _observe(repo, scope, "Alice works with Carol", when="2026-02-01T00:00:00Z"))

    bob = _named(repo, scope, "Bob", predicate="works_with")
    carol = _named(repo, scope, "Carol", predicate="works_with")
    assert bob.valid_to is None
    assert carol.valid_to is None
    assert bob.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert carol.valid_from == datetime(2026, 2, 1, tzinfo=UTC)


# -- switch off: exactly the old behaviour -----------------------------------


def test_switch_off_still_dates_relations_the_old_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEVERIAN_RELATION_EVENT_TIME", raising=False)
    repo = _repo(tmp_path)
    scope = _scope()
    _derive(repo, scope, _observe(repo, scope, "Alice works at Acme", when="2026-01-01T00:00:00Z"))
    _derive(repo, scope, _observe(repo, scope, "Alice works at Globex", when="2026-06-01T00:00:00Z"))

    acme = _named(repo, scope, "Acme")
    assert acme.valid_from is None  # no start time without the switch
    assert acme.valid_to is not None
    assert acme.valid_to != datetime(2026, 6, 1, tzinfo=UTC)  # ended at the wall clock
    assert _named(repo, scope, "Globex").valid_from is None


# -- the read boundary -------------------------------------------------------


def _windowed(repo: SQLiteRepository, scope: Scope) -> tuple[str, datetime, datetime]:
    """One relation with a closed window [start, end), plus the ids a graph query needs."""
    alice = repo.add_entity(scope, "person", "Alice")
    acme = repo.add_entity(scope, "organisation", "Acme")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = datetime(2026, 6, 1, tzinfo=UTC)
    repo.add_relation(scope, alice.id, "works_at", acme.id, valid_from=start, valid_to=end)
    return alice.id, start, end


def test_validity_is_half_open_at_both_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    entity_id, start, end = _windowed(repo, scope)

    assert _visible_at(repo, scope, entity_id, start) != []  # true at valid_from
    assert _visible_at(repo, scope, entity_id, end - timedelta(microseconds=1)) != []
    assert _visible_at(repo, scope, entity_id, end) == []  # false at valid_to
    assert _visible_at(repo, scope, entity_id, end + timedelta(days=1)) == []


def test_the_boundary_stays_closed_when_the_switch_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switch off: the old boundary convention survives, so nothing changes for existing stores."""
    monkeypatch.delenv("SEVERIAN_RELATION_EVENT_TIME", raising=False)
    repo = _repo(tmp_path)
    scope = _scope()
    entity_id, start, end = _windowed(repo, scope)

    assert _visible_at(repo, scope, entity_id, start) != []
    assert _visible_at(repo, scope, entity_id, end) != []  # still current at exactly valid_to
    assert _visible_at(repo, scope, entity_id, end + timedelta(days=1)) == []


def test_timezone_offsets_are_normalised_on_the_way_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window written in another zone is the same instant, so the boundary still bites."""
    monkeypatch.setenv("SEVERIAN_RELATION_EVENT_TIME", "1")
    repo = _repo(tmp_path)
    scope = _scope()
    entity_id, start, end = _windowed(repo, scope)

    plus_five = timezone(timedelta(hours=5))
    assert _visible_at(repo, scope, entity_id, start.astimezone(plus_five)) != []
    assert _visible_at(repo, scope, entity_id, end.astimezone(plus_five)) == []
