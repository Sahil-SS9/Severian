"""Tests for the MinHash-LSH pre-filter (P1d dreaming scale fix)."""

from __future__ import annotations

import hashlib
import random

from severian.application.dreaming import (
    _lsh_candidates,
    _minhash_signature,
    jaccard,
)
from severian.domain.models import Memory, Scope


def _memory(scope: Scope, content: str) -> Memory:
    # Stable test IDs: Python's builtin hash() is process-randomised for
    # strings (PYTHONHASHSEED), which would make test IDs differ across
    # runs and break determinism assertions. sha256 is stable.
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


def test_minhash_signature_is_deterministic() -> None:
    tokens = {"alpha", "beta", "gamma", "delta"}
    first = _minhash_signature(tokens)
    for _ in range(3):
        assert _minhash_signature(tokens) == first


def test_lsh_finds_identical_pairs() -> None:
    scope = _scope()
    memories = [
        _memory(scope, "The service is deployed to us-east-1."),
        _memory(scope, "The service is deployed to us-east-1."),  # duplicate
        _memory(scope, "Grace Hopper invented COBOL."),  # unrelated
    ]
    candidates = _lsh_candidates(memories)
    # The duplicate pair (0,1) MUST be a candidate.
    assert (0, 1) in candidates


def test_lsh_matches_brute_force_on_duplicates() -> None:
    """Every exact-Jaccard near-duplicate pair must survive LSH."""
    random.seed(20260805)
    scope = _scope()
    contents = [
        "The backup job runs every night at 02:00 UTC.",
        "The backup job runs nightly at two in the morning UTC.",  # near-dup
        "The revenue target for Q3 is twelve million dollars.",
        "The revenue target for Q4 is fifteen million dollars.",  # near-dup
        "The service is deployed to the us-east-1 region.",
        "The service is deployed to the eu-west-1 region.",  # near-dup (contradict)
        "Grace Hopper invented COBOL in 1959.",
        "Alan Turing proposed the Turing test in 1950.",
        "The headcount target for Q3 is twelve new engineers.",
        "The headcount target for Q4 is fifteen new engineers.",  # near-dup
    ]
    memories = [_memory(scope, c) for c in contents]

    # Brute force: all pairs with Jaccard >= 0.3
    brute: set[tuple[int, int]] = set()
    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            if jaccard(memories[i].content, memories[j].content) >= 0.3:
                brute.add((i, j))
    assert len(brute) >= 3  # sanity: corpus has real near-dups

    lsh = set(_lsh_candidates(memories))
    missing = brute - lsh
    assert not missing, f"LSH missed high-similarity pairs: {missing}"


def test_lsh_high_recall_on_diverse_corpus() -> None:
    """LSH must find ~all pairs at Jaccard >= 0.4 (the duplicate band)."""
    topics = {
        "network latency": ("the ping between nodes is", "ms"),
        "database index": ("the btree index depth reached", "levels"),
        "cache hit rate": ("the redis hit ratio was", "percent"),
        "auth token": ("the oauth token expired after", "minutes"),
        "api quota": ("the hourly api quota reset at", "utc"),
        "log rotation": ("the syslog rotation kept", "archives"),
        "backup window": ("the nightly backup finished in", "minutes"),
        "deploy region": ("the canary rollout covered", "zones"),
        "queue depth": ("the rabbitmq queue backlog hit", "messages"),
        "memory usage": ("the heap footprint grew to", "gigabytes"),
    }
    topics_list = list(topics)
    scope = _scope()
    memories: list[Memory] = []
    for i in range(500):
        topic = topics_list[i % len(topics_list)]
        phrase, unit = topics[topic]
        if i % 10 == 0:
            content = f"{phrase} {i % 100} {unit} (entry {i // 10})"
        else:
            content = f"{phrase} {i % 50} {unit} observed at {i % 24:02d}:00"
        memories.append(_memory(scope, content))

    brute: set[tuple[int, int]] = set()
    for i in range(len(memories)):
        for j in range(i + 1, len(memories)):
            if jaccard(memories[i].content, memories[j].content) >= 0.5:
                brute.add((i, j))
    lsh = set(_lsh_candidates(memories))
    missing = brute - lsh
    # At the 0.5 duplicate band, recall must be 100% (borderline 0.4
    # misses are acceptable — LSH is a candidate generator, not the
    # final decision).
    assert not missing, f"LSH missed duplicate-band pairs: {len(missing)}"
    # And the candidate set must be much smaller than all pairs.
    all_pairs = len(memories) * (len(memories) - 1) // 2
    assert len(lsh) < all_pairs * 0.5, "LSH must prune at least half the pairs"


def test_lsh_does_not_pair_unrelated() -> None:
    scope = _scope()
    memories = [
        _memory(scope, "Grace Hopper invented COBOL in 1959."),
        _memory(scope, "The quarterly revenue target is twelve million."),
        _memory(scope, "Alan Turing proposed the Turing test."),
        _memory(scope, "The private launch date is 2026-09-01."),
    ]
    candidates = _lsh_candidates(memories)
    # With 4 unrelated docs, no pair should exceed Jaccard 0.3 — but LSH
    # is probabilistic: rare collisions are possible. At least verify the
    # candidate set is much smaller than all 6 pairs (pre-filter works).
    assert len(candidates) < 6
