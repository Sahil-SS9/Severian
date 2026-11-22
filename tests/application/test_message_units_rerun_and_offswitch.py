"""0.6.4 F + G (S1 message units).

G: re-running the unit backfill adds no index rows (0.6.3 indexed the freshly built units under
new random ids instead of what publish_derivation stored).
F: with SEVERIAN_MESSAGE_UNITS off, units already in the store change nothing: search hits and
context are byte-identical to the same store before the units were added.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from severian.application.derivation import DerivationService
from severian.application.ingestion import IngestionService
from severian.application.projections import ProjectionCoordinator
from severian.application.retrieval import ContextAssembler, RetrievalService
from severian.application.service import BuildMemoryService
from severian.application.worker import DerivationWorker
from severian.domain.models import Collection, Scope
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

POISON = "POISON-TEXT"


class FakeGateway:
    model_id = "local/test-model"
    prompt_version = "prompt-v1"
    is_cloud = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": f"Fact: {content}", "confidence": 0.9}]


class PickyEmbedding(HashEmbedding):
    """Hash space, but refuses any text containing POISON once ``armed`` (like the 8000-char rejection)."""

    def __init__(self) -> None:
        super().__init__(dimension=64)
        self.armed = False
        self.calls = 0

    def encode(self, text: str) -> list[float]:
        self.calls += 1
        if self.armed and POISON in text:
            raise ValueError("embedding refused this text")
        return super().encode(text)


SCOPE = Scope(tenant_id="t1", profile_id="p1", collection_id="c1-t1", agent_id="a1", session_id="s1")


@pytest.fixture
def store(tmp_path: Path) -> Any:
    repo = SQLiteRepository(tmp_path / "severian.db")
    repo.initialize()
    repo.create_collection(Collection(id="c1-t1", tenant_id="t1", name="Primary"))
    fts = FTS5Index(tmp_path / "fts.db")
    embedding = PickyEmbedding()
    vector = LocalVectorIndex(tmp_path / "vectors.db", embedding)
    projection = ProjectionCoordinator(repo, fts, vector)
    service = BuildMemoryService(
        repository=repo,
        ingestion=IngestionService(repo),
        retrieval=RetrievalService(repo, fts, vector),
        projection=projection,
        derivation=DerivationService(repo),
        context=ContextAssembler(),
    )
    worker = DerivationWorker(repo, DerivationService(repo), projection)
    ns = dataclasses.make_dataclass("Store", ["repo", "fts", "vector", "embedding", "service", "worker", "path"])
    yield ns(repo, fts, vector, embedding, service, worker, tmp_path)
    fts.close()
    vector.close()
    repo.close()


def _seed(store: Any, texts: list[str]) -> list[str]:
    ids = []
    for text in texts:
        ids.append(store.service.ingest_observation(SCOPE, content=text, source={"provider": "test"}).observation_id)
    store.worker.drain(worker_id="seed", gateway=FakeGateway())
    return ids


# -- G: unit backfill re-run adds no rows -------------------------------------------------------


def _row_counts(path: Path) -> dict[str, int]:
    out: dict[str, int] = {}
    with sqlite3.connect(path / "vectors.db") as connection:
        out["vector"] = connection.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]
    with sqlite3.connect(path / "fts.db") as connection:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        out["fts"] = sum(
            connection.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 - fixed names
            for t in ("memory_fts", "unit_fts")
            if t in tables
        )
    return out


def test_message_unit_backfill_rerun_adds_no_index_rows(store: Any) -> None:
    from severian.application.message_units import migrate_message_units

    for turn in range(1, 4):
        store.service.ingest_observation(
            SCOPE,
            content=f"[2024/03/1{turn} (Fri)] user: question {turn} about seeds\nassistant: answer {turn}",
            source={"turn": turn},
        )
    store.worker.drain(worker_id="seed", gateway=FakeGateway())
    first = migrate_message_units(store.repo, SCOPE, confirm=True, rebuild=store.service._projection)
    assert first.units_written == 6
    counts_after_first = _row_counts(store.path)
    second = migrate_message_units(store.repo, SCOPE, confirm=True, rebuild=store.service._projection)
    assert second.units_written == 0
    assert _row_counts(store.path) == counts_after_first, "a re-run must add 0 index rows"
    active = {m.id for m in store.repo.list_memories(SCOPE)}
    assert store.vector.list_memory_ids(SCOPE) == active, "no vector row may point at a memory that is not stored"



# -- F: switch off = off --------------------------------------------------------------------------


def _snapshot(store: Any, queries: list[str]) -> list[dict[str, Any]]:
    out = []
    for query in queries:
        hits = store.service.search(SCOPE, query=query, limit=10)
        context = store.service.assemble_context(SCOPE, hits, max_chars=2000, query=query)
        out.append(
            {
                "hits": [
                    [h.memory_id, h.content, h.lexical_score, h.vector_score, h.fused_score] for h in hits
                ],
                "context": context,
            }
        )
    return out


def test_units_in_store_change_nothing_when_switch_is_off(store: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from severian.application.message_units import migrate_message_units

    monkeypatch.delenv("SEVERIAN_MESSAGE_UNITS", raising=False)
    turns = [
        "user: my sister Ana moved to Porto in May\nassistant: lovely, Porto is great in spring",
        "user: I planted tomatoes and basil on the balcony\nassistant: basil likes the sun",
        "user: the dentist appointment is on Friday at 3pm\nassistant: noted, Friday 3pm dentist",
        "user: we booked a ferry to Santander for August\nassistant: the ferry takes about a day",
        "user: Ana's birthday is the 12th of June\nassistant: I will remember the 12th of June",
    ]
    for turn, text in enumerate(turns, 1):
        store.service.ingest_observation(SCOPE, content=f"[2024/05/0{turn} (Wed)] {text}", source={"turn": turn})
    store.worker.drain(worker_id="seed", gateway=FakeGateway())
    queries = ["when is Ana's birthday", "where did my sister move", "ferry booking", "dentist Friday", "basil sun"]
    before = _snapshot(store, queries)

    monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
    report = migrate_message_units(store.repo, SCOPE, confirm=True, rebuild=store.service._projection)
    assert report.units_written >= 10
    with_units_on = _snapshot(store, queries)
    assert with_units_on != before, "sanity: with the switch on the units do take part"

    monkeypatch.delenv("SEVERIAN_MESSAGE_UNITS")
    after = _snapshot(store, queries)
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)



def _unit(content: str, memory_id: str) -> Any:
    from severian.application.message_units import MESSAGE_UNIT_KIND
    from severian.domain.models import Memory

    return Memory(
        id=memory_id, scope=SCOPE, kind=MESSAGE_UNIT_KIND, content=content, source_observation_ids=["o1"],
        model_id="severian.message-units", prompt_version="v1", confidence=1.0,
    )


def test_units_indexed_by_0_6_3_move_out_of_memory_fts_on_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """0.6.3 kept units in memory_fts (shared BM25 statistics); the first 0.6.4 open moves them, once."""
    monkeypatch.delenv("SEVERIAN_MESSAGE_UNITS", raising=False)
    path = tmp_path / "old.fts"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE VIRTUAL TABLE memory_fts USING fts5(memory_id UNINDEXED, tenant_id UNINDEXED,
            profile_id UNINDEXED, collection_id UNINDEXED, agent_id UNINDEXED, session_id UNINDEXED,
            content, kind, source_ids UNINDEXED, tokenize = 'porter unicode61')"""
        )
        rows = [("m1", "fact", "the kettle is blue"), ("u1", "message_unit", "user message 2: the kettle is red")]
        connection.executemany(
            "INSERT INTO memory_fts VALUES (?, 't1', 'p1', 'c1-t1', 'a1', 's1', ?, ?, 'o1')",
            [(memory_id, content, kind) for memory_id, kind, content in rows],
        )
    index = FTS5Index(path)
    try:
        assert [hit.memory_id for hit in index.search(SCOPE, "kettle")] == ["m1"]
        monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
        assert {hit.memory_id for hit in index.search(SCOPE, "kettle")} == {"m1", "u1"}
        assert index.count(SCOPE) == 2
        tables = dict(index.connection.execute("SELECT 'memory_fts', COUNT(*) FROM memory_fts").fetchall())
        assert tables["memory_fts"] == 1
    finally:
        index.close()
    reopened = FTS5Index(path)  # the move runs once; a second open changes nothing
    try:
        assert reopened.count(SCOPE) == 2
    finally:
        reopened.close()


def test_vector_rows_from_0_6_3_get_a_kind_column_and_rebuild_labels_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SEVERIAN_MESSAGE_UNITS", raising=False)
    path = tmp_path / "old.vec"
    embedding = HashEmbedding(dimension=64)
    index = LocalVectorIndex(path, embedding)
    unit = _unit("user message 2: the kettle is red", "u1")
    index.upsert(unit)
    index.close()
    with sqlite3.connect(path) as connection:  # what a 0.6.3 writer left: no kind column at all
        connection.execute("ALTER TABLE memory_vectors DROP COLUMN kind")
    index = LocalVectorIndex(path, embedding)
    try:
        assert [hit.memory_id for hit in index.search(SCOPE, "kettle red")] == ["u1"], "unlabelled until repaired"

        class _Repo:
            def list_memories(self, scope: Scope) -> list[Any]:
                return [unit]

        index.rebuild(_Repo(), SCOPE)  # type: ignore[arg-type]
        assert index.search(SCOPE, "kettle red") == []
        monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
        assert [hit.memory_id for hit in index.search(SCOPE, "kettle red")] == ["u1"]
    finally:
        index.close()


@pytest.mark.skipif(not __import__("os").environ.get("SEVERIAN_PG_DSN"), reason="SEVERIAN_PG_DSN not set")
def test_pgvector_switch_off_leaves_units_out(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import uuid

    from severian.domain.models import Memory
    from severian.infrastructure.pgvector_index import PGVectorIndex

    monkeypatch.delenv("SEVERIAN_MESSAGE_UNITS", raising=False)
    table = f"mv_v064_{uuid.uuid4().hex[:10]}"
    index = PGVectorIndex(os.environ["SEVERIAN_PG_DSN"], HashEmbedding(dimension=64), table_name=table)
    fact = Memory(
        scope=SCOPE, kind="fact", content="the kettle is blue", source_observation_ids=["o1"],
        model_id="m", prompt_version="v", confidence=1.0,
    )
    try:
        index.initialize()
        index.upsert_many([fact, _unit("user message 2: the kettle is red", "u-pg")])
        assert [hit.memory_id for hit in index.search(SCOPE, "kettle", limit=5)] == [fact.id]
        monkeypatch.setenv("SEVERIAN_MESSAGE_UNITS", "1")
        assert {hit.memory_id for hit in index.search(SCOPE, "kettle", limit=5)} == {fact.id, "u-pg"}
    finally:
        with index.engine.begin() as connection:
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}_meta"')
        index.close()
