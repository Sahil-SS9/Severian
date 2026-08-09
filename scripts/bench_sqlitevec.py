"""Task 4.3 evidence: sqlite-vec vs optimised pure-Python exact top-k.

Same corpus and query as scripts/bench_topk.py (50k x 256-dim float32)
so the numbers are directly comparable. sqlite-vec is used in its
default exact (brute-force) mode — an ANN index is NOT accepted for
speed alone; this measures the maintained-extension path.
"""
import json
import random
import sqlite3
import struct
import sys
import time
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


def _scope() -> Scope:
    return Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")


def main() -> None:
    random.seed(42)
    n = 50_000
    dim = 256
    scope = _scope()
    embedding = HashEmbedding(dimension=dim)

    # Build the pure-Python index (same as bench_topk.py).
    path = Path("/tmp/topk-bench.db")
    if path.exists():
        path.unlink()
    index = LocalVectorIndex(path, embedding)
    contents = [
        f"benchmark evidence entry {i} about the backup job and nightly report"
        for i in range(n)
    ]
    memories = [
        Memory(
            kind="fact", content=content, scope=scope,
            source_observation_ids=[f"obs-{i}"], model_id="bench",
            prompt_version="v1", confidence=0.9,
        )
        for i, content in enumerate(contents)
    ]
    t0 = time.perf_counter()
    index.upsert_many(memories)
    build_python_s = time.perf_counter() - t0

    # sqlite-vec index over the SAME vectors (read from the python db).
    vec_db = sqlite3.connect("/tmp/sqlitevec-bench.db")
    vec_db.enable_load_extension(True)
    sqlite_vec.load(vec_db)
    vec_db.execute("DROP TABLE IF EXISTS vec_items")
    vec_db.execute("DROP TABLE IF EXISTS meta")
    vec_db.execute("CREATE TABLE meta (memory_id TEXT PRIMARY KEY, content TEXT)")
    vec_db.execute(
        "CREATE VIRTUAL TABLE vec_items USING vec0(memory_id TEXT PRIMARY KEY, vector float[256])"
    )
    t0 = time.perf_counter()
    rows = index.connection.execute("SELECT memory_id, vector, content FROM memory_vectors").fetchall()
    for memory_id, blob, content in rows:
        vec_db.execute(
            "INSERT INTO vec_items(memory_id, vector) VALUES (?, ?)",
            (memory_id, blob),
        )
        vec_db.execute(
            "INSERT OR REPLACE INTO meta (memory_id, content) VALUES (?, ?)",
            (memory_id, content),
        )
    vec_db.commit()  # persist before timing/close (implicit-txn rollback trap)
    build_vec_s = time.perf_counter() - t0

    query = "backup job nightly report"
    query_vector = embedding.encode(query)
    q_blob = struct.pack(f"<{dim}f", *query_vector)

    # Reference: pure-Python bounded top-k (the 4.2 path).
    def python_topk(limit: int = 20) -> list[str]:
        return [h.memory_id for h in index.search(scope, query, limit=limit)]

    # sqlite-vec exact top-k via vec0 KNN (brute-force exact).
    def vec_topk(limit: int = 20) -> list[str]:
        rows = vec_db.execute(
            "SELECT memory_id, distance FROM vec_items WHERE vector MATCH ? "
            "ORDER BY distance LIMIT ?",
            (q_blob, limit),
        ).fetchall()
        return [row[0] for row in rows]

    # Deterministic wrapper: sqlite-vec breaks ties by internal order,
    # violating the total-order contract (score desc, content,
    # memory_id). Re-sort the small k with the SAME key as the Python
    # path — k is tiny, so the wrapper is O(k log k) and exact.
    def vec_topk_deterministic(limit: int = 20) -> list[str]:
        rows = vec_db.execute(
            "SELECT memory_id, distance FROM vec_items WHERE vector MATCH ? LIMIT ?",
            (q_blob, limit),
        ).fetchall()
        scored = [(rid, 1.0 - dist, _META.get(rid, "")) for rid, dist in rows]
        scored.sort(key=lambda item: (-item[1], item[2], item[0]))
        return [r for r, _s, _c in scored]

    _META: dict[str, str] = dict(
        vec_db.execute("SELECT memory_id, content FROM meta").fetchall()
    )

    def bench(fn, repeat=5) -> float:
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t0)
        return min(times)

    python_ids = python_topk()
    vec_ids = vec_topk()
    vec_det_ids = vec_topk_deterministic()
    overlap = len(set(python_ids) & set(vec_ids))

    report = {
        "n_vectors": n,
        "dimension": dim,
        "build_python_seconds": round(build_python_s, 3),
        "build_sqlitevec_seconds": round(build_vec_s, 3),
        "query_python_seconds": round(bench(python_topk), 5),
        "query_sqlitevec_seconds": round(bench(vec_topk), 5),
        "top20_overlap": overlap,
        "top20_equal": python_ids == vec_ids,
        "deterministic_wrapper_matches_python_order": python_ids == vec_det_ids,
        "sqlite_vec_version": "0.1.9",
        "mode": "exact brute-force KNN (vec0)",
    }
    print(json.dumps(report, indent=2))
    Path("/tmp/sqlitevec-bench-result.json").write_text(json.dumps(report, indent=2))
    index.close()
    vec_db.close()


if __name__ == "__main__":
    main()
