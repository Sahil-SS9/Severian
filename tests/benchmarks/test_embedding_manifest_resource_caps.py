"""The embedding bake-off must prove the locked CPU-first resource budget."""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPO_ROOT / "docs" / "benchmarks" / "manifest-embedding-v1.json"


def test_neural_embedding_lanes_are_hard_capped_to_four_cpu_and_four_gib() -> None:
    """Every sentence-transformers lane must run under the locked 4 CPU / 4 GiB limit."""
    manifest = json.loads(MANIFEST_PATH.read_text())
    neural = [
        provider
        for provider in manifest["providers"]
        if provider["config"].get("runtime") == "sentence-transformers"
    ]

    assert neural
    assert all(provider["config"].get("cpus") == 4 for provider in neural)
    assert all(provider["config"].get("mem_mb") == 4096 for provider in neural)
