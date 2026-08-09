"""Tests for the zero-debt 5.2 dense-degeneration repair.

Evidence driving the repair (Task 5.1 profile):
- shared-scaffold corpora collapsed to candidate_ratio 1.000 with the
  max bucket holding the ENTIRE corpus (template fixture, 10k docs);
- empty/stopword-only documents were silently skipped, so duplicate
  pairs among them were invisible (stopwordy recall 0.166).

The repair (final, evidence-led): exact-duplicate condensation (digest
groups) + the ORIGINAL stopword-filtered signature + token caching.
DF-pruning was tried and REJECTED during 5.2 because it regressed
recall on the diverse corpus (2 missed pairs vs 0 with the original
signature) and exposed a pathological fixed-salt band interaction.
"""

from __future__ import annotations

import hashlib
import random

from severian.application.dreaming import (
    _candidate_batches,
    _lsh_candidates,
    jaccard,
)
from severian.domain.models import Memory, Scope


def _memory(scope: Scope, content: str) -> Memory:
    return Memory(
        id=f"m-{hashlib.sha256(content.encode('utf-8')).hexdigest()[:16]}",
        scope=scope,
        kind="fact",
        content=content,
        source_observation_ids=["obs-1"],
        model_id="test",
        prompt_version="test-v1",
        confidence=0.5,
    )


def _scope() -> Scope:
    return Scope(tenant_id="t1", profile_id="p1", collection_id="c1", agent_id="a1", session_id="s1")


def test_duplicate_condensation_collapses_identical_contents() -> None:
    """Identical contents are direct candidates via digest groups."""
    scope = _scope()
    memories = [
        _memory(scope, "The nightly backup completed at 02:00."),
        _memory(scope, "The nightly backup completed at 02:00."),
        _memory(scope, "The nightly backup completed at 02:00."),
        _memory(scope, "The database migration applied cleanly."),
    ]
    candidates = set(_lsh_candidates(memories))
    # All pairs among the triple must be candidates (exact duplicates).
    for a, b in [(0, 1), (0, 2), (1, 2)]:
        assert (a, b) in candidates
    # The unrelated doc must NOT pair with the duplicates.
    assert not any(3 in pair for pair in candidates)


def test_dense_template_corpus_no_longer_collapses() -> None:
    """Shared-scaffold corpus: candidate ratio must drop well below 1.0.

    The 5.1 failure mode: every doc shared the scaffold tokens, so all
    docs landed in the same buckets (ratio 1.000). Condensation collapses
    identical contents to one representative; the field tokens then drive
    the blocking.
    """
    scope = _scope()
    jobs = ["alpha", "beta", "gamma", "delta"]
    times = ["02:00", "03:15", "04:30", "23:45"]
    rng = random.Random(42)
    corpus = [
        f"The nightly backup job {rng.choice(jobs)} completed at {rng.choice(times)} "
        f"with exit code {rng.choice(['0', '1'])} and wrote {rng.choice(['1.2GB', '800MB'])}"
        for _ in range(200)
    ]
    memories = [_memory(scope, c) for c in corpus]
    candidates = set(_lsh_candidates(memories))
    total_pairs = 200 * 199 // 2
    ratio = len(candidates) / total_pairs
    # Condensation alone must massively prune; target < 25% of all pairs.
    assert ratio < 0.25, f"candidate ratio {ratio:.3f} — dense degeneration returned"
    # And recall must stay perfect for exact duplicates.
    by_content: dict[str, list[int]] = {}
    for i, c in enumerate(corpus):
        by_content.setdefault(c, []).append(i)
    for idxs in by_content.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                assert (idxs[a], idxs[b]) in candidates, "exact duplicate missed"


def test_scaffold_heavy_pairs_survive_condensation() -> None:
    """Pairs sharing ONLY scaffold but differing in rare tokens still surface.

    Condensation only collapses IDENTICAL contents; near-duplicates that
    share the scaffold but differ in one field remain candidates via the
    original stopword-filtered signature (recall gate preserved).
    """
    scope = _scope()
    a = "the nightly backup job completed successfully at 02:00 utc"
    b = "the nightly backup job completed unsuccessfully at 02:00 utc"
    memories = [_memory(scope, a), _memory(scope, b), _memory(scope, "grace hopper invented cobol")]
    assert jaccard(a, b) >= 0.6  # contradiction band
    candidates = set(_lsh_candidates(memories))
    assert (0, 1) in candidates, "scaffold-only contradiction pair dropped by pre-filter"


def test_empty_content_duplicates_are_candidates() -> None:
    """Identical stopword-only docs must not be silently skipped.

    5.1 evidence: stopwordy fixture recall 0.166 — empty-token docs
    were `continue`d out of the bucket pass entirely. Condensation pairs
    identical contents by digest, so identical stopword-only docs pair.
    (Empty content is rejected by the domain model, so the fixture uses
    stopword-only content — the real-world form of the gap.)
    """
    scope = _scope()
    memories = [
        _memory(scope, "the and of to in is was at"),
        _memory(scope, "the and of to in is was at"),
        _memory(scope, "grace hopper invented cobol"),
    ]
    candidates = set(_lsh_candidates(memories))
    assert (0, 1) in candidates  # identical stopword-only docs
    # Stopword-only vs contentful must NOT pair (jaccard 0) and not crash.
    assert not any(2 in pair for pair in candidates if 0 in pair or 1 in pair)


def test_batch_generator_equals_materialised() -> None:
    """_candidate_batches with any batch_size yields the same pair set."""
    scope = _scope()
    rng = random.Random(7)
    corpus = [
        f"the nightly backup job {rng.choice(['alpha', 'beta'])} completed "
        f"at {rng.choice(['02:00', '03:15'])}" for _ in range(60)
    ]
    memories = [_memory(scope, c) for c in corpus]
    full = set(_lsh_candidates(memories))
    for batch_size in [1, 7, 10_000]:
        got: set[tuple[int, int]] = set()
        for batch in _candidate_batches(memories, batch_size=batch_size):
            got.update(batch)
        assert got == full, f"batch_size={batch_size} diverges"


def test_lsh_high_recall_on_diverse_corpus_still_holds() -> None:
    """The original 5.1 recall gate: brute-force pairs at >= 0.5 must survive."""
    topics = {
        "network latency": ("the ping between nodes is", "ms"),
        "database index": ("the btree index depth reached", "levels"),
        "cache hit rate": ("the redis hit ratio was", "percent"),
    }
    topics_list = list(topics)
    scope = _scope()
    memories: list[Memory] = []
    for i in range(120):
        topic = topics_list[i % len(topics_list)]
        phrase, unit = topics[topic]
        content = f"{phrase} {i % 50} {unit} observed at {i % 24:02d}:00"
        memories.append(_memory(scope, content))
    brute: set[tuple[int, int]] = set()
    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            if jaccard(memories[i].content, memories[j].content) >= 0.5:
                brute.add((i, j))
    lsh = set(_lsh_candidates(memories))
    missing = brute - lsh
    assert not missing, f"LSH missed duplicate-band pairs: {len(missing)}"
    all_pairs = len(memories) * (len(memories) - 1) // 2
    assert len(lsh) < all_pairs * 0.5, "LSH must prune at least half the pairs"
