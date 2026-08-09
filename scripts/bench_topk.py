"""Micro-benchmark: bounded top-k vs full sort, and vector unpacking options.

zero-debt 4.2 asks for evidence on FFI-independent Python alternatives
(array/memoryview/list) before choosing. This is a standalone evidence
script — run it directly, not via pytest.
"""
import json
import random
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


def _scope() -> Scope:
    return Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")


def main() -> None:
    random.seed(42)
    n = 50_000
    scope = _scope()
    embedding = HashEmbedding(dimension=256)

    # Build an index of n distinct-ish contents.
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
    build_s = time.perf_counter() - t0

    query = "backup job nightly report"
    query_vector = embedding.encode(query)

    def bench(fn, repeat=5) -> float:
        times = []
        for _ in range(repeat):
            t0 = time.perf_counter()
            fn()
            times.append(time.perf_counter() - t0)
        return min(times)

    # 1) Full-sort reference (pre-4.2 path).
    rows = index.connection.execute(
        "SELECT memory_id, vector, content FROM memory_vectors"
    ).fetchall()
    blobs = [(rid, blob, content) for rid, blob, content in rows]

    def full_sort() -> list[str]:
        scored = []
        for memory_id, blob, content in blobs:
            vec = list(struct.unpack(f"<{embedding.dimension}f", blob))
            score = sum(a * b for a, b in zip(query_vector, vec, strict=True))
            scored.append((memory_id, score, content))
        scored.sort(key=lambda item: (-item[1], item[2], item[0]))
        return [item[0] for item in scored[:20]]

    # 2) Bounded top-k (4.2 path) — list unpack.
    import heapq

    def bounded_list() -> list[str]:
        scored = []
        for memory_id, blob, content in blobs:
            vec = list(struct.unpack(f"<{embedding.dimension}f", blob))
            score = sum(a * b for a, b in zip(query_vector, vec, strict=True))
            scored.append((memory_id, score, content))
        return [item[0] for item in heapq.nsmallest(
            20, scored, key=lambda item: (-item[1], item[2], item[0])
        )]

    # 3) Bounded top-k — array('f') unpack + memoryview dot.
    from array import array

    def bounded_array() -> list[str]:
        qv = array("f", query_vector)
        scored = []
        for memory_id, blob, content in blobs:
            vec = array("f")
            vec.frombytes(blob)
            score = sum(a * b for a, b in zip(qv, vec, strict=True))
            scored.append((memory_id, score, content))
        return [item[0] for item in heapq.nsmallest(
            20, scored, key=lambda item: (-item[1], item[2], item[0])
        )]

    # 4) Opt-in NumPy lane (SEVERIAN_OPT_NUMPY=1): matrix decode + float64
    # matmul + lexsort over the same total key.
    def bounded_numpy() -> list[str]:
        import numpy as np

        n = len(blobs)
        memory_ids = [item[0] for item in blobs]
        contents = [item[2] for item in blobs]
        matrix = np.frombuffer(
            b"".join(item[1] for item in blobs), dtype="<f4"
        ).reshape(n, embedding.dimension).astype(np.float64)
        scores = matrix @ np.asarray(query_vector, dtype=np.float64)
        order = np.lexsort((np.asarray(memory_ids), np.asarray(contents), -scores))
        return [memory_ids[i] for i in order[:20]]

    full_ids = full_sort()
    bounded_ids = bounded_list()
    array_ids = bounded_array()
    numpy_ids = bounded_numpy()
    assert full_ids == bounded_ids == array_ids == numpy_ids, (
        "bounded top-k must equal full sort on all paths"
    )

    full_s = bench(full_sort)
    list_s = bench(bounded_list)
    array_s = bench(bounded_array)
    numpy_s = bench(bounded_numpy)

    report = {
        "n_vectors": n,
        "dimension": embedding.dimension,
        "build_seconds": round(build_s, 3),
        "full_sort_seconds": round(full_s, 4),
        "bounded_list_seconds": round(list_s, 4),
        "bounded_array_seconds": round(array_s, 4),
        "numpy_opt_in_seconds": round(numpy_s, 4),
        "speedup_full_vs_list": round(full_s / list_s, 2),
        "speedup_list_vs_array": round(list_s / array_s, 2) if array_s else None,
        "speedup_full_vs_numpy": round(full_s / numpy_s, 1) if numpy_s else None,
        "equality_proven_across_all_paths": True,
    }
    print(json.dumps(report, indent=2))
    out = Path("/tmp/topk-bench-result.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"wrote {out}")
    index.close()


if __name__ == "__main__":
    main()
