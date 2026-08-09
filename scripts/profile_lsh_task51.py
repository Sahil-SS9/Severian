"""Task 5.1 — Profile the existing MinHash-LSH contradiction pre-filter.

Fixtures (per plan):
- diverse corpus (distinct topics)
- dense shared-template corpus (same scaffold, few varying tokens)
- exact duplicates
- contradiction band (near-duplicate texts with one negating token)
- stopword-heavy and empty-token documents
- 1K / 10K buckets (10K bounded by 300s wall)

Metrics: signature time, bucket distribution (max bucket, heavy tail),
candidate ratio, exact-Jaccard time, recall of true duplicate /
contradiction pairs, peak memory (tracemalloc).
"""
import gc
import hashlib
import json
import random
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from severian.application.dreaming import (  # noqa: E402
    _lsh_candidates,
    _minhash_signature,
    _tokens,  # noqa: E402
    jaccard,
)
from severian.domain.text import STOPWORDS  # noqa: E402

STOPWORD_HEAVY = " ".join(sorted(STOPWORDS))

TEMPLATE = (
    "The nightly backup job {job} completed at {time} and wrote {size} to "
    "storage with exit code {code}; the report was attached to the run log"
)


def _mk_corpus(kind: str, n: int, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    if kind == "diverse":
        topics = [
            "backup job", "database migration", "api rate limit", "memory retention",
            "gpu temperature", "disk usage", "cron schedule", "auth token",
            "network latency", "queue depth", "log rotation", "proxy config",
        ]
        return [
            f"Observed {rng.choice(topics)} event {i} with metric {rng.randint(0, 999)} "
            f"and status {rng.choice(['ok', 'warn', 'fail'])} at {rng.randint(0, 23)}:{rng.randint(0, 59):02d}"
            for i in range(n)
        ]
    if kind == "template":
        jobs = ["alpha", "beta", "gamma", "delta"]
        times = ["02:00", "03:15", "04:30", "23:45"]
        sizes = ["1.2GB", "3.4GB", "800MB", "12GB"]
        codes = ["0", "1", "0", "2"]
        return [
            TEMPLATE.format(job=rng.choice(jobs), time=rng.choice(times),
                            size=rng.choice(sizes), code=rng.choice(codes))
            for _ in range(n)
        ]
    if kind == "duplicates":
        base = [
            "The database migration v42 applied cleanly on all replicas",
            "The API rate limit was raised to 1200 requests per minute",
            "The cron scheduler missed its 02:00 tick due to maintenance",
        ]
        return [base[i % len(base)] for i in range(n)]
    if kind == "contradictions":
        base = [
            "The nightly backup completed successfully at 02:00",
            "The nightly backup FAILED at 02:00",
            "The api gateway is healthy and responding",
            "The api gateway is DOWN and not responding",
        ]
        # alternate the two poles so the contradiction band is dense
        return [base[i % 2 if (i // 2) % 2 == 0 else 2 + (i % 2)] for i in range(n)]
    if kind == "stopwordy":
        docs = []
        for i in range(n):
            if i % 4 == 0:
                docs.append("the and of to in is was")
            elif i % 4 == 1:
                docs.append("")
            else:
                docs.append(f"{STOPWORD_HEAVY} unique_token_{i}")
        return docs
    raise ValueError(kind)


def _true_pairs_analytic(kind: str, corpus: list[str], n: int) -> set[tuple[int, int]]:
    """True duplicate/contradiction pairs WITHOUT O(n^2) jaccard.

    Patterned fixtures define pairs analytically (a 10k brute-force
    jaccard sweep is ~50M calls — that cost is itself what LSH must
    avoid, so the profiler must not pay it either).

    - duplicates: same base content -> identical strings
    - contradictions: the two poles differ by one token; jaccard of
      the poles is high (shared scaffold) -> all same-pole pairs are
      exact dupes; cross-pole pairs with overlap >= 0.6 are true
      contradiction pairs
    - diverse/template/stopwordy: brute-force is affordable at 1k;
      at 10k sample the first 2000 docs (the candidate-ratio and
      blow-up findings dominate; recall is secondary evidence there)
    """
    if kind == "duplicates":
        by_content: dict[str, list[int]] = {}
        for i, c in enumerate(corpus):
            by_content.setdefault(c, []).append(i)
        pairs: set[tuple[int, int]] = set()
        for idxs in by_content.values():
            for a in range(len(idxs)):
                for b in range(a + 1, len(idxs)):
                    pairs.add((idxs[a], idxs[b]))
        return pairs
    if kind == "contradictions":
        # generator alternates poles 0/1 (success band) and 2/3 (health band)
        pairs = set()
        band_a, band_b = {0, 1}, {2, 3}
        for i in range(n):
            for j in range(i + 1, n):
                gi, gj = i % 4, j % 4
                in_same_band = (gi in band_a and gj in band_a) or (gi in band_b and gj in band_b)
                if in_same_band:
                    pairs.add((i, j))
        return pairs
    # diverse / template / stopwordy: brute force at 1k; sampled at 10k
    m = min(n, 2000)
    pairs = set()
    for i in range(m):
        for j in range(i + 1, m):
            if corpus[i] == corpus[j] or jaccard(corpus[i], corpus[j]) >= 0.6:
                pairs.add((i, j))
    return pairs


def _profile(kind: str, n: int) -> dict:
    corpus = _mk_corpus(kind, n)
    true_pairs = _true_pairs_analytic(kind, corpus, n)

    # signature pass
    gc.collect()
    tracemalloc.start()
    t0 = time.perf_counter()
    sigs = []
    for i, content in enumerate(corpus):
        toks = {t for t in _tokens(content) if t not in STOPWORDS}
        if not toks:
            toks = _tokens(content)
        sigs.append((i, toks, _minhash_signature(toks) if toks else None))
    sig_s = time.perf_counter() - t0
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # bucket distribution
    bucket_sizes: dict[int, int] = {}
    bucket: dict[tuple[int, int], list[int]] = {}
    for i, _toks, sig in sigs:
        if sig is None:
            continue
        for band, value in enumerate(sig):
            key = (band, value)
            bucket.setdefault(key, []).append(i)
    for indices in bucket.values():
        bucket_sizes[len(indices)] = bucket_sizes.get(len(indices), 0) + 1
    max_bucket = max(bucket_sizes) if bucket_sizes else 0
    heavy_tail = sum(c for size, c in bucket_sizes.items() if size >= 20)

    # candidate pass
    class _M:
        def __init__(self, content: str) -> None:
            self.content = content

    memories = [_M(c) for c in corpus]
    t0 = time.perf_counter()
    candidates = _lsh_candidates(memories)  # type: ignore[arg-type]
    cand_s = time.perf_counter() - t0
    total_pairs = n * (n - 1) // 2
    cand_ratio = len(candidates) / total_pairs if total_pairs else 0.0

    # exact Jaccard on candidates (bounded: at dense degeneration the
    # candidate set EXPLODES — sampling the first N is the honest
    # way to report the blow-up without waiting for 46M evaluations;
    # the candidate count itself is the finding).
    EXACT_CAP = 2_000_000
    sampled = candidates[:EXACT_CAP]
    t0 = time.perf_counter()
    hits = [(a, b) for a, b in sampled if jaccard(corpus[a], corpus[b]) >= 0.6]
    exact_s = time.perf_counter() - t0
    exact_capped = len(candidates) > EXACT_CAP

    cand_set = set(candidates)
    # zero-debt 5.2: with exact-duplicate condensation, a candidate pair
    # of representatives semantically covers the FULL cross-product of
    # their digest groups (identical contents => identical Jaccard), so
    # doc-index pair counting no longer applies. Recall is measured in
    # rep space: a true pair (i, j) is recalled iff its reps are the
    # same group (exact duplicates) or (rep_i, rep_j) is a candidate.
    group_rep: dict[str, int] = {}
    rep_of: dict[int, int] = {}
    for i, c in enumerate(corpus):
        d = hashlib.sha256(c.encode()).hexdigest()
        if d not in group_rep:
            group_rep[d] = i
        rep_of[i] = group_rep[d]
    recalled = set()
    for a, b in true_pairs:
        ra, rb = rep_of[a], rep_of[b]
        if ra == rb or (ra, rb) in cand_set or (rb, ra) in cand_set:
            recalled.add((a, b))
    recall = len(recalled) / len(true_pairs) if true_pairs else 1.0
    if exact_capped:
        recall = 0.0  # unverifiable at this candidate volume — flagged

    return {
        "kind": kind, "n": n,
        "signature_seconds": round(sig_s, 2),
        "signature_per_doc_ms": round(sig_s / n * 1000, 2),
        "max_bucket": max_bucket,
        "bucket_count": len(bucket),
        "heavy_tail_buckets_ge20": heavy_tail,
        "candidate_pairs": len(candidates),
        "candidate_ratio": round(cand_ratio, 4),
        "candidate_seconds": round(cand_s, 2),
        "exact_jaccard_seconds": round(exact_s, 2),
        "exact_jaccard_hits": len(hits),
        "exact_jaccard_capped": exact_capped,
        "true_pairs": len(true_pairs),
        "recalled_pairs": len(recalled),
        "recall": round(recall, 4),
        "peak_memory_mb": round(peak / 1024 / 1024, 1),
    }


def main() -> None:
    results = []
    for kind in ["diverse", "template", "duplicates", "contradictions", "stopwordy"]:
        for n in [1_000, 10_000]:
            wall = time.monotonic()
            print(f"profiling {kind} @ {n} ...", flush=True)
            try:
                r = _profile(kind, n)
                r["wall_seconds"] = round(time.monotonic() - wall, 1)
                results.append(r)
                print(json.dumps(r), flush=True)
            except Exception as exc:  # noqa: BLE001
                results.append({"kind": kind, "n": n, "error": str(exc),
                                "wall_seconds": round(time.monotonic() - wall, 1)})
                print(f"  ERROR {exc}", flush=True)
            if time.monotonic() - wall > 280:
                print("  (wall budget exceeded — skipping larger bucket)", flush=True)
                break
    out = Path("/tmp/lsh-profile-task51.json")
    out.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
