"""CLI for validating provider benchmark manifests.

A live benchmark requires explicitly registered provider adapters and is
therefore intentionally invoked from Python with ``BenchmarkRunner``.
This command provides a deterministic CI-friendly manifest validation
entry point.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from severian.benchmarks.manifest import load_manifest

app = typer.Typer(help="Severian provider benchmark tooling.")


@app.callback()
def main() -> None:
    """Validate and inspect provider benchmark manifests."""


@app.command()
def validate(manifest: Path = typer.Option(..., "--manifest", exists=True, readable=True)) -> None:
    """Validate a versioned manifest and print its benchmark contract."""
    loaded = load_manifest(manifest)
    typer.echo(
        json.dumps(
            {
                "manifest_id": loaded.manifest_id,
                "schema_version": loaded.schema_version,
                "providers": [provider.id for provider in loaded.providers],
                "queries": len(loaded.queries),
                "metrics": loaded.metrics,
                "require_external_providers": loaded.release_gate.require_external_providers,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    app()
