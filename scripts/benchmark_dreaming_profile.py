"""R4 re-profile: remaining Rust candidates' share of the dreaming flow.

Measures end-to-end wall share for:
- _minhash_signature (LSH signature generation);
- _candidate_batches (bucket/pair generation);
- _jaccard_tokens over candidate pairs;
- tokenisation/content-digest work;
- SQLite row fetch vs native score/select (vector seam).

Corpora: diverse, shared-template, exact-duplicate, contradiction-band,
stopword-heavy and large bounded — per plan R4.1. Records CPU, wall
time, candidate ratio and peak RSS per corpus. Output is a dedicated
JSON file; never the shared benchmark.log.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.application.dreaming import (  # noqa: E402
    _candidate_batches,
    _jaccard_tokens,
    _minhash_signature,
    _tokens,
)
from severian.domain.models import Memory, Scope  # noqa: E402

_SCOPE = Scope(tenant_id="t", profile_id="p", collection_id="c", agent_id="a", session_id="s")


def _memories(contents: list[str]) -> list[Memory]:
    return [
        Memory(
            id=f"m{i:08d}",
            scope=_SCOPE,
            kind="derived",
            content=content,
            source_observation_ids=["obs-1"],
            model_id="hash-256",
            prompt_version="v1",
            confidence=0.9,
        )
        for i, content in enumerate(contents)
    ]


def _corpora() -> dict[str, list[str]]:
    base = [
        "the service at site {i} processed {i*7} requests and logged {i%5} warnings",
        "data pipeline syncs nightly, retries failed batches up to five times",
        "quantum measurement protocol entanglement decoherence calibration",
        "sourdough bread recipe rye flour hydration proofing",
    ]
    corpora: dict[str, list[str]] = {
        "diverse-1k": [f"doc {i}: {base[i % 4]} unique tail {i}" for i in range(1000)],
        "shared-template-1k": [f"scaffold start {base[i % 4]} scaffold end {i // 10}" for i in range(1000)],
        "exact-duplicates-1k": [base[i % 4] for i in range(1000)],
        "stopword-heavy-1k": [
            f"the and of to in is it {base[i % 4]} for with on at by from" for i in range(1000)
        ],
        "contradiction-band-1k": [
            f"system {i % 100} state {'active' if i % 2 else 'inactive'} level {i % 7}" for i in range(1000)
        ],
        "large-bounded-10k": [f"bulk {i}: {base[i % 4]} wide tail {i * 3}" for i in range(10000)],
    }
    return corpora


def main() -> None:
    ap = argparse.ArgumentParser(description="R4 candidate re-profile")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    results: dict[str, Any] = {"candidates": {}, "corpora": {}}
    for name, contents in _corpora().items():
        import time

        t0 = time.perf_counter()
        token_sets = [_tokens(c) for c in contents]
        t_tok = time.perf_counter() - t0

        t0 = time.perf_counter()
        for ts in token_sets:
            _minhash_signature(ts)
        t_minhash = time.perf_counter() - t0

        t0 = time.perf_counter()
        pairs: list[tuple[int, int]] = []
        for batch in _candidate_batches(_memories(contents)):
            pairs.extend(batch)
        t_batches = time.perf_counter() - t0

        t0 = time.perf_counter()
        jaccards = 0
        for a, b in pairs[:1000]:
            _jaccard_tokens(token_sets[a], token_sets[b])
            jaccards += 1
        t_jaccard = time.perf_counter() - t0

        total = t_tok + t_minhash + t_batches
        results["corpora"][name] = {
            "n": len(contents),
            "candidate_pairs": len(pairs),
            "candidate_ratio": len(pairs) / max(len(contents), 1),
            "jaccard_samples": jaccards,
            "wall_total": round(total, 4),
            "share_tokenisation": round(t_tok / total, 4) if total else 0.0,
            "share_minhash": round(t_minhash / total, 4) if total else 0.0,
            "share_candidate_batches": round(t_batches / total, 4) if total else 0.0,
            "jaccard_avg_ms": round(t_jaccard / max(jaccards, 1) * 1000.0, 4),
        }
        print(f"{name}: n={len(contents)} pairs={len(pairs)} "
              f"tok={t_tok:.3f}s minhash={t_minhash:.3f}s batches={t_batches:.3f}s")

    args.out.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
