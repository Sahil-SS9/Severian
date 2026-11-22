"""0.6.4 D: ``severian rebuild-vectors`` repairs a live index in place.

0.6.3 rebuilt into a new file and swapped it in with ``os.replace``; every running gateway and the
drain kept the OLD inode open and kept writing to it. It also refused the whole store on one text
over 8000 characters. The in-place repair writes the missing vectors into the SAME file in chunks
(each chunk one transaction, so an interrupted run keeps what it finished and a re-run continues),
removes rows that belong to no active memory, and has a dry run that only counts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from severian.application.worker import DerivationWorker
from severian.cli.app import cli
from severian.composition import build_bundle
from severian.domain.models import Collection, Scope
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex

SCOPES = [
    Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1"),
    Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s2"),
]


class _Counter:
    calls = 0


def CountingEmbedding(fail_after: int | None = None) -> Any:  # noqa: N802 - reads like the class it stands in for
    """The store's own hash space (same fingerprint), counting encodes; optionally dies after N."""
    embedding: Any = HashEmbedding()
    counter = _Counter()
    plain = embedding.encode

    def encode(text: str) -> list[float]:
        if fail_after is not None and counter.calls >= fail_after:
            raise ConnectionError("sidecar went away")
        counter.calls += 1
        return plain(text)

    embedding.encode = encode
    embedding.counter = counter
    return embedding


def _store(tmp_path: Path, per_scope: int = 12, scopes: list[Scope] = SCOPES) -> dict[str, Path]:
    paths = {"db": tmp_path / "s.db", "fts": tmp_path / "s.fts", "vec": tmp_path / "s.vec"}
    bundle = build_bundle(database=paths["db"], fts=paths["fts"], vectors=paths["vec"])
    bundle.repository.create_collection(Collection(id="c1", tenant_id="t1", name="c"))
    for scope in scopes:
        for i in range(per_scope):
            bundle.service.ingest_observation(scope, content=f"{scope.session_id} memory {i} about rivers", source={})
    DerivationWorker(bundle.repository, bundle.derivation, bundle.projection).drain(gateway=bundle.gateway)
    bundle.close()
    return paths


def _lose_vectors(paths: dict[str, Path], keep: int) -> None:
    """Simulate the incident: most rows gone, one stale row, one orphan."""
    with sqlite3.connect(paths["vec"]) as connection:
        ids = [r[0] for r in connection.execute("SELECT memory_id FROM memory_vectors ORDER BY memory_id")]
        connection.executemany("DELETE FROM memory_vectors WHERE memory_id = ?", [(i,) for i in ids[keep:]])
        connection.execute("UPDATE memory_vectors SET content = 'stale text' WHERE memory_id = ?", (ids[0],))
        connection.execute(
            "INSERT INTO memory_vectors (memory_id, tenant_id, profile_id, collection_id, agent_id, session_id, "
            "content, vector) SELECT 'orphan-id', tenant_id, profile_id, collection_id, agent_id, session_id, "
            "content, vector FROM memory_vectors LIMIT 1"
        )


def _active_ids(paths: dict[str, Path]) -> set[str]:
    with sqlite3.connect(paths["db"]) as connection:
        return {
            r[0]
            for r in connection.execute(
                "SELECT id FROM records WHERE record_type='memory' "
                "AND json_extract(payload,'$.superseded_by_id') IS NULL"
            )
        }


def _vector_ids(paths: dict[str, Path]) -> set[str]:
    with sqlite3.connect(paths["vec"]) as connection:
        return {r[0] for r in connection.execute("SELECT memory_id FROM memory_vectors")}


def _repository(paths: dict[str, Path]) -> Any:
    from severian.infrastructure.sqlite import SQLiteRepository

    repository = SQLiteRepository(paths["db"])
    repository.initialize()
    return repository


def test_plan_counts_without_writing(tmp_path: Path) -> None:
    from severian.infrastructure.rebuild import plan_vector_repair

    paths = _store(tmp_path)
    _lose_vectors(paths, keep=5)
    before = paths["vec"].read_bytes()
    repository = _repository(paths)
    index = LocalVectorIndex(paths["vec"], HashEmbedding())
    try:
        plan = plan_vector_repair(index, repository, None)
    finally:
        index.close()
        repository.close()
    assert plan.active == 24
    assert plan.missing == 19
    assert plan.stale == 1
    assert plan.orphans == 1
    assert plan.to_encode == 20
    assert paths["vec"].read_bytes() == before


def test_in_place_repair_fills_the_same_file(tmp_path: Path) -> None:
    from severian.infrastructure.rebuild import repair_vectors_in_place

    paths = _store(tmp_path)
    _lose_vectors(paths, keep=5)
    inode = os.stat(paths["vec"]).st_ino
    repository = _repository(paths)
    embedding = CountingEmbedding()
    index = LocalVectorIndex(paths["vec"], embedding)
    try:
        result = repair_vectors_in_place(index, repository, None, chunk=4)
    finally:
        index.close()
        repository.close()
    assert result.status == "repaired"
    assert result.encoded == 20 and result.deleted == 1 and result.failed == ()
    assert embedding.counter.calls == 20, "only missing or stale rows are encoded"
    assert os.stat(paths["vec"]).st_ino == inode, "repaired in place: running openers see the rows"
    assert _vector_ids(paths) == _active_ids(paths)


def test_interrupted_repair_keeps_finished_chunks_and_resumes(tmp_path: Path) -> None:
    from severian.infrastructure.rebuild import repair_vectors_in_place

    paths = _store(tmp_path, per_scope=24, scopes=SCOPES[:1])
    _lose_vectors(paths, keep=4)
    repository = _repository(paths)
    try:
        flaky = CountingEmbedding(fail_after=9)
        index = LocalVectorIndex(paths["vec"], flaky)
        try:
            with pytest.raises(ConnectionError):
                repair_vectors_in_place(index, repository, None, chunk=4)
        finally:
            index.close()
        # Two chunks of 4 committed before the 10th encode failed. The first re-encoded the stale
        # row (smallest id, so first in order), so 7 memories gained a row: 4 kept + 7 = 11.
        assert len(_vector_ids(paths) & _active_ids(paths)) == 4 + 7, "the finished chunks stay committed"
        again = CountingEmbedding()
        index = LocalVectorIndex(paths["vec"], again)
        try:
            result = repair_vectors_in_place(index, repository, None, chunk=4)
        finally:
            index.close()
    finally:
        repository.close()
    assert result.status == "repaired"
    assert again.counter.calls == 21 - 8, "the re-run only encodes what is still missing"
    assert _vector_ids(paths) == _active_ids(paths)


def test_cli_rebuild_vectors_dry_run_then_in_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEVERIAN_EMBEDDING", "hash")
    paths = _store(tmp_path)
    _lose_vectors(paths, keep=3)
    inode = os.stat(paths["vec"]).st_ino
    flags = ["--database", str(paths["db"]), "--fts", str(paths["fts"]), "--vectors", str(paths["vec"])]
    runner = CliRunner()
    dry = runner.invoke(cli, ["rebuild-vectors", *flags, "--all-scopes", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    plan = json.loads(dry.output.strip().splitlines()[-1])
    assert plan["status"] == "planned"
    assert plan["plan"]["missing"] == 21 and plan["plan"]["orphans"] == 1
    assert len(_vector_ids(paths)) == 4
    done = runner.invoke(cli, ["rebuild-vectors", *flags, "--all-scopes"])
    assert done.exit_code == 0, done.output
    body = json.loads(done.output.strip().splitlines()[-1])
    assert body["status"] == "repaired"
    assert body["mode"] == "in-place"
    assert os.stat(paths["vec"]).st_ino == inode
    assert _vector_ids(paths) == _active_ids(paths)



@pytest.mark.skipif(not os.environ.get("SEVERIAN_PG_DSN"), reason="SEVERIAN_PG_DSN not set")
def test_pgvector_in_place_repair_fills_missing_rows_and_drops_orphans() -> None:
    import uuid

    from severian.domain.models import Memory
    from severian.infrastructure.pgvector_index import PGVectorIndex
    from severian.infrastructure.rebuild import plan_vector_repair, repair_vectors_in_place

    scope = SCOPES[0]
    memories = [
        Memory(scope=scope, kind="fact", content=f"pg repair memory {i}", source_observation_ids=["o1"],
               model_id="m", prompt_version="v", confidence=1.0)
        for i in range(7)
    ]
    orphan = Memory(scope=scope, kind="fact", content="gone", source_observation_ids=["o1"],
                    model_id="m", prompt_version="v", confidence=1.0)

    class _Repo:
        def list_memories(self, wanted: Scope) -> list[Memory]:
            return list(memories) if wanted == scope else []

    table = f"mv_v064_{uuid.uuid4().hex[:10]}"
    index = PGVectorIndex(os.environ["SEVERIAN_PG_DSN"], HashEmbedding(), table_name=table)
    try:
        index.initialize()
        index.upsert_many([*memories[:2], orphan])
        plan = plan_vector_repair(index, _Repo(), scope)  # type: ignore[arg-type]
        assert (plan.active, plan.present, plan.missing, plan.orphans) == (7, 2, 5, 1)
        result = repair_vectors_in_place(index, _Repo(), scope, chunk=2)  # type: ignore[arg-type]
        assert result.status == "repaired" and result.verified
        assert result.encoded == 5 and result.deleted == 1
        assert index.list_memory_ids(scope) == {m.id for m in memories}
    finally:
        with index.engine.begin() as connection:
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}"')
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}_meta"')
        index.close()
