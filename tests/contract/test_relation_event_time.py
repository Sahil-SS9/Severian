"""S3 conformance: relation validity from event time, on both backends.

The application layer decides WHEN a relation starts and ends (the source
message's event time), and passes that instant to ``publish_derivation`` as
``relation_valid_from``. These tests prove each backend persists and compares
the instant the same way: a new relation starts there, a replaced one ends
there, mixed zones stay ordered, and an earlier statement never closes a later
fact. Without the keyword nothing changes (no backfill, no invention).
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from severian.domain.models import Collection, Observation, Relation, Scope
from severian.infrastructure.postgresql import PostgreSQLBackend
from severian.infrastructure.sqlite import SQLiteRepository

PG_DSN = os.environ.get("SEVERIAN_PG_DSN", "")


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request: pytest.FixtureRequest, tmp_path: Path):
    """Both backends: the temporal write path must be identical, not merely similar."""
    if request.param == "postgres":
        if not PG_DSN:
            pytest.skip("SEVERIAN_PG_DSN not set; skipping live PostgreSQL conformance")
        repo = PostgreSQLBackend(dsn=PG_DSN)
        os.environ.setdefault("SEVERIAN_PG_ALLOW_RESET", "1")
        repo.reset(acknowledge=True)
    else:
        repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    yield repo
    repo.close()


def _unique_scope() -> Scope:
    """Scope unique per test so parametrized runs don't collide in shared Postgres."""
    return Scope(
        tenant_id=f"t-{uuid.uuid4().hex[:8]}",
        profile_id="p1",
        collection_id=f"c-{uuid.uuid4().hex[:8]}",
        agent_id="a1",
        session_id="s1",
    )


def _publish(
    backend: Any,
    scope: Scope,
    content: str,
    obj: str,
    *,
    started: datetime | None = None,
    predicate: str = "works_at",
) -> Relation:
    """Publish one observation's graph, then return the relation it created or supported."""
    observation = backend.add_observation(Observation(scope=scope, content=content))
    backend.publish_derivation(
        scope,
        entities=[("person", "Alice"), ("organisation", obj)],
        relations=[("Alice", predicate, obj)],
        memories=[],
        source_observation_id=observation.id,
        **({} if started is None else {"relation_valid_from": started}),
    )
    return _named(backend, scope, obj, predicate)


def _named(backend: Any, scope: Scope, obj: str, predicate: str = "works_at") -> Relation:
    names = {entity.id: entity.name.casefold() for entity in backend.list_entities(scope)}
    found = [
        relation
        for relation in backend.list_relations(scope)
        if relation.predicate == predicate and names.get(relation.object_id) == obj.casefold()
    ]
    assert len(found) == 1, found
    return found[0]


def _instant(value: Any) -> datetime:
    """One UTC instant from a stored or audited timestamp."""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def _audit_rows(backend: Any, scope: Scope, action: str) -> list[Any]:
    return [row for row in backend.list_audit(scope) if row.action == action]


# -- start time --------------------------------------------------------------


def test_a_new_relation_takes_the_start_time_it_is_given(backend: Any) -> None:
    scope = _unique_scope()
    started = datetime(2026, 3, 1, 10, tzinfo=UTC)
    relation = _publish(backend, scope, "Alice works at Acme", "Acme", started=started)

    assert relation.valid_from == started
    assert relation.valid_to is None


def test_a_relation_without_a_given_start_time_is_unchanged(backend: Any) -> None:
    """No keyword: the derived relation stays undated, exactly as before."""
    scope = _unique_scope()
    relation = _publish(backend, scope, "Alice works at Acme", "Acme")

    assert relation.valid_from is None
    assert relation.valid_to is None


# -- end time ----------------------------------------------------------------


def test_a_replacement_ends_the_old_value_at_the_new_statements_instant(backend: Any) -> None:
    scope = _unique_scope()
    first = datetime(2026, 1, 1, tzinfo=UTC)
    second = datetime(2026, 6, 1, tzinfo=UTC)
    acme = _publish(backend, scope, "Alice works at Acme", "Acme", started=first)
    globex = _publish(backend, scope, "Alice works at Globex", "Globex", started=second)

    ended = _named(backend, scope, "Acme")
    assert ended.valid_from == first
    assert ended.valid_to == second  # the newer statement's instant, not the wall clock
    assert globex.valid_from == second
    assert acme.id == ended.id

    audit = _audit_rows(backend, scope, "relation.end")
    assert [row.target_id for row in audit] == [acme.id]
    assert _instant(audit[0].metadata["valid_to"]) == second


def test_mixed_time_zones_are_stored_as_ordered_instants(backend: Any) -> None:
    scope = _unique_scope()
    # 09:00+05:00 is 04:00Z and 06:00-02:00 is 08:00Z: same calendar day, four hours apart.
    first = datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=5)))
    second = datetime(2026, 1, 1, 6, tzinfo=timezone(timedelta(hours=-2)))
    _publish(backend, scope, "Alice works at Acme", "Acme", started=first)
    _publish(backend, scope, "Alice works at Globex", "Globex", started=second)

    acme = _named(backend, scope, "Acme")
    globex = _named(backend, scope, "Globex")
    assert _instant(acme.valid_from) == datetime(2026, 1, 1, 4, tzinfo=UTC)
    assert acme.valid_to is not None
    assert _instant(acme.valid_to) == datetime(2026, 1, 1, 8, tzinfo=UTC)
    assert _instant(globex.valid_from) == datetime(2026, 1, 1, 8, tzinfo=UTC)


def test_an_earlier_statement_leaves_the_current_value_open(backend: Any) -> None:
    """Late-arriving older news is history; the fact that started later stays current."""
    scope = _unique_scope()
    _publish(backend, scope, "Alice works at Acme", "Acme", started=datetime(2026, 6, 1, tzinfo=UTC))
    _publish(backend, scope, "Alice works at Globex", "Globex", started=datetime(2025, 1, 1, tzinfo=UTC))

    assert _named(backend, scope, "Acme").valid_to is None
    assert _named(backend, scope, "Globex").valid_from == datetime(2025, 1, 1, tzinfo=UTC)
    assert _audit_rows(backend, scope, "relation.end") == []


def test_a_compatible_predicate_never_ends_the_other_value(backend: Any) -> None:
    scope = _unique_scope()
    bob = _publish(backend, scope, "Alice works with Bob", "Bob", predicate="works_with")
    carol = _publish(
        backend,
        scope,
        "Alice works with Carol",
        "Carol",
        started=datetime(2026, 2, 1, tzinfo=UTC),
        predicate="works_with",
    )

    assert _named(backend, scope, "Bob", "works_with").valid_to is None
    assert carol.valid_to is None
    assert bob.valid_from is None  # no start time given: unchanged
    assert _audit_rows(backend, scope, "relation.end") == []


def test_an_undated_existing_relation_is_ended_but_not_backfilled(backend: Any) -> None:
    """A relation written before the switch keeps its empty start; only its end is dated."""
    scope = _unique_scope()
    _publish(backend, scope, "Alice works at Acme", "Acme")  # no start time: the old shape
    replaced_at = datetime(2026, 6, 1, tzinfo=UTC)
    _publish(backend, scope, "Alice works at Globex", "Globex", started=replaced_at)

    acme = _named(backend, scope, "Acme")
    assert acme.valid_from is None
    assert acme.valid_to == replaced_at
