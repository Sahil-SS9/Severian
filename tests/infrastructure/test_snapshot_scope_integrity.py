"""DATA-01: snapshot import must reject cross-scope references.

The snapshot validator previously confirmed that references EXIST but
not that they are SAME-SCOPE. A memory in scope X could reference an
observation in scope Y, and a relation could reference an entity in a
different scope. Imports must reject such records — references across
scopes would break the 5-D isolation boundary.

Fixtures are generated from a REAL repository export so the records
match the actual schema (no hand-built payloads to drift).
"""

from __future__ import annotations

import json

import pytest

from severian.domain.models import Collection, Memory, Observation, Scope
from severian.infrastructure.snapshot import validate_snapshot
from severian.infrastructure.sqlite import SQLiteRepository


def _scope(tenant: str = "t1") -> Scope:
    return Scope(
        tenant_id=tenant,
        profile_id="p1",
        collection_id="c1",
        agent_id="a1",
        session_id="s1",
    )


@pytest.fixture
def _exported_records(tmp_path) -> list[dict]:
    """Real SQLite store with one observation + one memory in scope t1."""
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1", tenant_id="t1", name="Primary"))
    scope = _scope("t1")
    obs = repo.add_observation(Observation(scope=scope, content="Ada founded a company"))
    repo.add_memory(
        Memory(
            id="mem-1",
            scope=scope,
            kind="fact",
            content="Ada founded a company",
            source_observation_ids=[obs.id],
            model_id="test",
            prompt_version="v1",
            confidence=1.0,
        )
    )
    payload = json.loads(repo.export_json())
    repo.close()
    records = payload["records"] if isinstance(payload, dict) and "records" in payload else payload
    return records


def test_memory_cross_scope_source_observation_is_rejected(_exported_records) -> None:
    obs = next(r for r in _exported_records if r["record_type"] == "observation")
    mem = next(r for r in _exported_records if r["record_type"] == "memory")
    # Move the memory to a different tenant (leave its source obs in t1).
    mem = json.loads(json.dumps(mem))  # deep copy
    mem["tenant_id"] = "t2"
    mem["payload"]["scope"]["tenant_id"] = "t2"
    with pytest.raises(ValueError, match="different scope"):
        validate_snapshot([obs, mem])


def test_memory_same_scope_source_observation_is_accepted(_exported_records) -> None:
    validate_snapshot(_exported_records)  # must not raise
