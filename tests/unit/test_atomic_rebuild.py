"""Atomic vector rebuild contract (zero-debt plan, Task 2.3).

- Rebuild is side-by-side: live index untouched during build.
- Swap is atomic; pre-swap index preserved as rollback.
- Failure/abort before swap leaves the original untouched.
- Verification contract: count, digest, dimension/fingerprint, canary
  retrieval, deterministic ranking.
- Only owned temp artifacts are removed on failure.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


from severian.domain.models import Memory, Scope  # noqa: E402
from severian.infrastructure.rebuild import (  # noqa: E402
    rebuild_sqlite_vectors,
    verify_candidate,
)
from severian.infrastructure.vector import HashEmbedding, LocalVectorIndex  # noqa: E402


def _scope() -> Scope:
    return Scope(
        tenant_id="t1", profile_id="p1", collection_id="c1",
        agent_id="a1", session_id="s1",
    )


class _MemoryRepository:
    """Duck-typed canonical store (rebuild only needs list_memories)."""

    def __init__(self, memories: list[Memory]):
        self._memories = memories

    def list_memories(self, scope: Scope) -> list[Memory]:
        return [
            m for m in self._memories
            if m.scope.tenant_id == scope.tenant_id
            and m.scope.profile_id == scope.profile_id
            and m.scope.collection_id == scope.collection_id
            and m.scope.agent_id == scope.agent_id
            and m.scope.session_id == scope.session_id
        ]


def _memories(n: int = 12, scope: Scope | None = None) -> list[Memory]:
    scope = scope or _scope()
    return [
        Memory(
            id=f"mem-{i:03d}",
            kind="fact",
            content=f"the backup job for region {chr(97 + i % 26)} runs at {2 + i % 20:02d}:00 utc",
            scope=scope,
            source_observation_ids=[f"obs-{i:03d}"],
            model_id="test/hasher",
            prompt_version="test-v1",
            confidence=0.9,
        )
        for i in range(n)
    ]


def test_rebuild_swaps_and_verifies(tmp_path: Path) -> None:
    scope = _scope()
    repo = _MemoryRepository(_memories())
    live = tmp_path / "vectors.db"
    live_index = LocalVectorIndex(live, HashEmbedding())
    for memory in repo.list_memories(scope):
        live_index.upsert(memory)
    live_index.close()

    result = rebuild_sqlite_vectors(live, repo, scope, HashEmbedding())

    assert result.status == "swapped"
    assert result.verification is not None
    assert result.verification.passed
    assert result.verification.canonical_count == 12
    assert result.verification.vector_count == 12
    assert result.verification.digest_match is True
    assert result.verification.deterministic is True
    # Rollback file preserved.
    assert result.rollback_path is not None
    assert Path(result.rollback_path).exists()

    # Live file is the rebuilt index and is usable.
    reopened = LocalVectorIndex(live, HashEmbedding())
    assert reopened.count(scope) == 12
    reopened.close()


def test_verification_detects_count_mismatch(tmp_path: Path) -> None:
    scope = _scope()
    repo = _MemoryRepository(_memories())
    candidate = LocalVectorIndex(tmp_path / "cand.db", HashEmbedding())
    # Only ingest half the canonical memories.
    for memory in repo.list_memories(scope)[:6]:
        candidate.upsert(memory)

    verification = verify_candidate(candidate, repo, scope, HashEmbedding(), repo.list_memories(scope))
    assert verification.vector_count == 6
    assert verification.canonical_count == 12
    assert not verification.passed
    candidate.close()


def test_verification_detects_digest_mismatch(tmp_path: Path) -> None:
    scope = _scope()
    memories = _memories()
    repo = _MemoryRepository(memories)
    candidate = LocalVectorIndex(tmp_path / "cand.db", HashEmbedding())
    # Ingest all but with one memory's content altered (stale content).
    for memory in memories:
        candidate.upsert(
            Memory(
                id=memory.id, kind=memory.kind,
                content=memory.content + " STALE", scope=memory.scope,
                source_observation_ids=memory.source_observation_ids,
                model_id=memory.model_id, prompt_version=memory.prompt_version,
                confidence=memory.confidence,
            )
        )

    verification = verify_candidate(candidate, repo, scope, HashEmbedding(), memories)
    assert verification.vector_count == 12
    assert verification.digest_match is False
    assert not verification.passed
    candidate.close()


def test_aborted_rebuild_leaves_original_untouched(tmp_path: Path) -> None:
    """A failing verification must not touch the live index."""
    scope = _scope()
    repo = _MemoryRepository(_memories())
    live = tmp_path / "vectors.db"
    live_index = LocalVectorIndex(live, HashEmbedding())
    for memory in repo.list_memories(scope):
        live_index.upsert(memory)
    live_index.close()
    before = live.read_bytes()

    # Force failure: repository yields MORE memories than the candidate
    # can hold — simulate by making the repo return memories with
    # content that the candidate index cannot digest... simplest honest
    # approach: use a repo whose list_memories raises mid-verification.
    class _BoomRepository(_MemoryRepository):
        def list_memories(self, scope: Scope) -> list[Memory]:
            raise RuntimeError("boom")

    result = rebuild_sqlite_vectors(live, _BoomRepository([]), scope, HashEmbedding())

    assert result.status == "failed"
    assert result.error is not None
    # Original untouched, no candidate/rollback litter.
    assert live.read_bytes() == before
    assert not list(tmp_path.glob("*.rebuild-*"))
    assert not list(tmp_path.glob("*.bak-*"))


def test_candidate_failure_cleans_only_owned_artifacts(tmp_path: Path) -> None:
    """A failed rebuild removes its own temp file, keeps the live index."""
    scope = _scope()
    repo = _MemoryRepository(_memories())
    live = tmp_path / "vectors.db"
    live_index = LocalVectorIndex(live, HashEmbedding())
    for memory in repo.list_memories(scope):
        live_index.upsert(memory)
    live_index.close()
    before = live.read_bytes()

    # A repository whose memories break ingestion at encode time.
    class _BadRepo(_MemoryRepository):
        def list_memories(self, scope: Scope) -> list[Memory]:
            # Valid model object, but content that blows up at encode:
            # rebuild() calls embedding.encode(memory.content) with a
            # non-str type.
            return [
                Memory(
                    id="x", kind="fact", content="bad", scope=scope,
                    source_observation_ids=["obs-0"], model_id="m",
                    prompt_version="v", confidence=0.5,
                )
            ]

    # Force encode failure: swap the embedding for one that raises.
    class _BoomEmbedding:
        dimension = 256

        def encode(self, text: str) -> list[float]:
            raise RuntimeError("encode boom")

    result = rebuild_sqlite_vectors(live, _BadRepo([]), scope, _BoomEmbedding())

    assert result.status == "failed"
    assert live.read_bytes() == before
    assert not list(tmp_path.glob("*.rebuild-*"))
    assert not list(tmp_path.glob("*.bak-*"))


def test_rebuild_no_keep_rollback_removes_old(tmp_path: Path) -> None:
    scope = _scope()
    repo = _MemoryRepository(_memories())
    live = tmp_path / "vectors.db"
    live_index = LocalVectorIndex(live, HashEmbedding())
    for memory in repo.list_memories(scope):
        live_index.upsert(memory)
    live_index.close()

    result = rebuild_sqlite_vectors(live, repo, scope, HashEmbedding(), keep_rollback=False)

    assert result.status == "swapped"
    assert result.rollback_path is None
    assert not list(tmp_path.glob("*.bak-*"))
