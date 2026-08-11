"""Standalone benchmark CLI selection must use the canonical resolver."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import run_benchmark  # noqa: E402


def test_qwen_benchmark_selection_constructs_named_qwen_provider() -> None:
    manifest = SimpleNamespace(
        providers=[
            SimpleNamespace(
                id="severian-qwen",
                vendor_reported_only=False,
                kind="severian",
                backend="sqlite",
            )
        ]
    )
    args = SimpleNamespace(embedding="qwen", pg_dsn=None)

    adapters = run_benchmark.build_adapters(manifest, args)
    adapter = adapters["severian-qwen"]
    try:
        assert type(adapter._embedding).__name__ == "QwenEmbedding"
        assert adapter._embedding.dimension == 384
    finally:
        adapter.close()
