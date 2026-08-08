"""Shared benchmark provenance collector (quality rule 4, zero-debt plan).

Every benchmark evidence record must contain: candidate SHA, dirty-patch
identity if any, corpus/manifest hash, exact model revision, runtime
versions, hardware fingerprint, command, seed, repetitions and skipped
lanes. Single implementation, imported by every benchmark script — no
duplicated resolver logic.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any


def collect_provenance(
    repo_root: Path,
    command: list[str],
    seed: int | str,
    model_revision: str = "n/a (hash embedding baseline)",
    corpus_fingerprint: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """Return the full provenance record for a benchmark evidence file.

    Args:
        repo_root: repository root used for git queries.
        command: the exact argv that launched the benchmark.
        seed: benchmark seed (int or str).
        model_revision: embedding model + revision, or n/a for hash.
        corpus_fingerprint: optional callable returning the corpus hash;
            when omitted the caller must add its own corpus hash key.
    """
    dirty = subprocess.run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        cwd=repo_root,
    ).stdout.strip()
    cpu_model = "unknown"
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    cpu_model = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    runtime: dict[str, str] = {
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
    try:
        import sqlite3

        runtime["sqlite"] = sqlite3.sqlite_version
    except Exception:
        pass
    for mod in ("psycopg", "pgvector", "onnxruntime", "sentence_transformers", "numpy"):
        try:
            m = __import__(mod)
            runtime[mod] = getattr(m, "__version__", "unknown")
        except Exception:
            runtime[mod] = "unavailable"
    record: dict[str, Any] = {
        "git_sha": subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=repo_root
        ).stdout.strip(),
        "dirty_patch": dirty if dirty else "clean",
        "model_revision": model_revision,
        "runtime_versions": runtime,
        "hardware": {"cpu": cpu_model, "cores": os.cpu_count()},
        "command": command,
        "seed": seed,
    }
    if corpus_fingerprint is not None:
        record["generated_corpus_sha256"] = corpus_fingerprint()
    return record


def corpus_hash(generator: Callable[[int], list[tuple[str, str]]], max_bucket: int) -> str:
    """Hash the deterministic corpus generator output (quality rule 4)."""
    return hashlib.sha256(
        json.dumps([c for c in generator(max_bucket)], sort_keys=True).encode()
    ).hexdigest()


def sys_command() -> list[str]:
    """Return the exact argv of the current process."""
    return list(sys.argv)
