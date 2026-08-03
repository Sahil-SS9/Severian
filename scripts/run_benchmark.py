#!/usr/bin/env python3
"""Run the Severian provider-comparison benchmark and write evidence.

Usage:
    python scripts/run_benchmark.py --manifest docs/benchmarks/manifest-v1.json \
        [--out docs/benchmarks/evidence/2026-08-02] \
        [--chroma-venv /tmp/bench-compare-venv] \
        [--pg-dsn postgresql://severian:severian@127.0.0.1:54329/severian]

The script:
1. validates the manifest;
2. builds adapters for every provider in the manifest;
3. runs a pre-flight canary (ingest one doc, query it, verify roundtrip);
4. runs the full benchmark and evaluates the release gate;
5. writes raw per-query traces, the result JSON, manifest SHA, commit
   SHA and environment fingerprint to the evidence directory.

This script is the ONLY sanctioned way to produce release benchmark
numbers (per docs/provider-benchmark.md). Numbers are never copied
into source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from severian.benchmarks.adapters import LexicalBaselineAdapter, SeverianAdapter  # noqa: E402
from severian.benchmarks.manifest import load_manifest  # noqa: E402
from severian.benchmarks.runner import (  # noqa: E402
    BenchmarkRunner,
    ProviderAdapter,
    to_jsonable,
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def git_branch() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, cwd=REPO_ROOT
        )
        return result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def build_adapters(manifest: Any, args: argparse.Namespace) -> dict[str, ProviderAdapter]:
    """Construct one adapter per LIVE manifest provider id.

    Vendor-reported providers (``vendor_reported_only``) are never run;
    their claimed metrics are carried in the evidence as citations only.
    """
    adapters: dict[str, ProviderAdapter] = {}
    for provider in manifest.providers:
        pid = provider.id
        if provider.vendor_reported_only:
            continue  # never fabricate an adapter for vendor-reported claims
        if provider.kind == "severian":
            backend = provider.backend or "sqlite"
            if backend in {"postgresql", "postgres"}:
                dsn = args.pg_dsn or os.environ.get("SEVERIAN_PG_DSN")
                if not dsn:
                    raise SystemExit(f"provider {pid}: postgresql backend requires --pg-dsn or SEVERIAN_PG_DSN")
                # The benchmark PG lane is a scratch database; reset it so
                # every run measures exactly this corpus (see adapter).
                os.environ["SEVERIAN_PG_ALLOW_RESET"] = "1"
                adapters[pid] = SeverianAdapter(
                    provider_id=pid, backend="postgresql", dsn=dsn, reset_db=True
                )
            else:
                adapters[pid] = SeverianAdapter(provider_id=pid, backend="sqlite")
        elif pid == "chroma" or (
            provider.kind == "external" and provider.vendor and "chroma" in provider.vendor.lower()
        ):
            try:
                from severian.benchmarks.adapters.chroma import ChromaAdapter

                adapters[pid] = ChromaAdapter(provider_id=pid)
            except ImportError as exc:
                venv_hint = ""
                if args.chroma_venv:
                    venv_hint = f" (chroma venv: {args.chroma_venv})"
                # Optional lane: skip gracefully so CI and environments
                # without the isolated chroma venv can still run the live
                # gate on the platform lanes. The evidence records the
                # skip as a provider_failure so it is never mistaken for
                # a passed lane.
                print(f"provider {pid}: chromadb not importable{venv_hint}; skipping lane ({exc})")
                continue
        elif pid.startswith("baseline") or provider.id == "baseline-lexical":
            adapters[pid] = LexicalBaselineAdapter(provider_id=pid)
        else:
            raise SystemExit(
                f"provider {pid}: no adapter implemented; "
                "cannot fabricate benchmark numbers for an unimplemented adapter"
            )
    return adapters


def canary(adapters: dict[str, ProviderAdapter]) -> None:
    """Pre-flight: prove each adapter can ingest+query one document."""
    for pid, adapter in adapters.items():
        adapter.ingest("canary-doc", "The benchmark canary flies at dawn.", scope="t0/p0/c0/a0/s0")
        response = adapter.query("canary", limit=5, scope="t0/p0/c0/a0/s0")
        if not any(item.get("document_id") == "canary-doc" for item in response.items):
            raise RuntimeError(f"canary FAILED for provider {pid}: roundtrip did not return canary-doc")
        print(f"  canary OK: {pid}")
        adapter.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--chroma-venv", type=Path)
    parser.add_argument("--pg-dsn", type=str)
    args = parser.parse_args()

    # If a chroma venv is supplied, make its site-packages importable so
    # the ChromaAdapter can load chromadb from the isolated environment
    # without polluting Severian's dependency set.
    if args.chroma_venv:
        site_packages = (
            args.chroma_venv
            / "lib"
            / f"python{sys.version_info.major}.{sys.version_info.minor}"
            / "site-packages"
        )
        if not site_packages.exists():
            # uv venvs sometimes use a different layout; try lib/python3*/site-packages
            candidates = sorted(
                (args.chroma_venv / "lib").glob("python*/site-packages")
            ) if (args.chroma_venv / "lib").exists() else []
            if candidates:
                site_packages = candidates[-1]
        if site_packages.exists():
            sys.path.insert(0, str(site_packages))
            print(f"chroma site-packages: {site_packages}")
        else:
            print(f"WARNING: chroma site-packages not found under {args.chroma_venv}")

    manifest_path = args.manifest.resolve()
    manifest = load_manifest(manifest_path)
    print(f"manifest: {manifest.manifest_id} (schema v{manifest.schema_version})")
    print(f"providers: {[p.id for p in manifest.providers]}")
    print(f"queries: {len(manifest.queries)} | docs: {len(manifest.corpus.documents)}")

    adapters = build_adapters(manifest, args)
    print("\npre-flight canary:")
    canary(adapters)

    # Rebuild adapters (canary consumed/closed them) for the real run.
    adapters = build_adapters(manifest, args)
    runner = BenchmarkRunner(manifest=manifest, adapters=adapters, allow_missing=True)
    print("\nrunning benchmark...")
    result = runner.run()
    payload = to_jsonable(result)

    # Evidence fingerprint
    fingerprint = {
        "manifest": {
            "path": str(manifest_path),
            "sha256": sha256_file(manifest_path),
            "id": manifest.manifest_id,
        },
        "commit": {"sha": git_head(), "branch": git_branch()},
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "pg_dsn_set": bool(args.pg_dsn or os.environ.get("SEVERIAN_PG_DSN")),
            "chroma_venv": str(args.chroma_venv) if args.chroma_venv else None,
        },
        "adapters": {
            pid: (adapter.describe() if hasattr(adapter, "describe") else {})
            for pid, adapter in adapters.items()
        },
        "vendor_reported": [
            {
                "id": provider.id,
                "vendor": provider.vendor,
                "deployment": provider.deployment,
                "model": provider.model.id,
                "reference_url": provider.config.get("reference_url"),
                "access_date": datetime.now(UTC).date().isoformat(),
            }
            for provider in manifest.providers
            if provider.vendor_reported_only
        ],
        "run_started_at": datetime.now(UTC).isoformat(),
    }
    evidence_root = REPO_ROOT / "docs" / "benchmarks" / "evidence" / datetime.now(UTC).date().isoformat()
    out_dir = (args.out or evidence_root).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    result_path = out_dir / "result.json"
    trace_path = out_dir / "traces.json"
    fingerprint_path = out_dir / "fingerprint.json"

    result_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    traces: dict[str, object] = {
        pid: {"provider_id": pid, "queries": payload["results"][pid]["raw"]}
        for pid in payload["results"]
    }
    trace_path.write_text(json.dumps(traces, indent=2, sort_keys=True), encoding="utf-8")
    fingerprint_path.write_text(json.dumps(fingerprint, indent=2, sort_keys=True), encoding="utf-8")

    print("\n=== RESULTS ===")
    print(json.dumps(payload, indent=2, sort_keys=True))
    print(f"\ngate_passed: {result.gate_passed}")
    print(f"gate_failures: {result.gate_failures or 'none'}")
    print(f"\nevidence written to: {out_dir}")
    print(f"  result.json sha256: {sha256_file(result_path)}")
    print(f"  traces.json sha256: {sha256_file(trace_path)}")
    print(f"  fingerprint.json sha256: {sha256_file(fingerprint_path)}")


if __name__ == "__main__":
    main()
