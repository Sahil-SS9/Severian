"""``python -m severian.mcp`` — stdio MCP server entry point.

Composes the canonical on-disk wiring (SQLite repository + FTS5 +
vector index) once, wires the MCP server, and runs the stdio loop.
All resources are closed on exit. Writes go exclusively through the
service; this entry point never duplicates writes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from severian.composition import build_bundle
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import LocalVectorIndex
from severian.mcp.server import SeverianMCPServer


class _LocalHashingGateway:
    """Deterministic local derivation gateway (no network)."""

    model_id: str = "local/mcp-hasher"
    prompt_version: str = "mcp-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]


def _build_server(
    *,
    database: Path,
    fts: Path,
    vectors: Path,
) -> tuple[SeverianMCPServer, SQLiteRepository, FTS5Index, LocalVectorIndex]:
    # ARCH-01: use the SINGLE composition root — the MCP runner must not
    # hand-wire repository + indexes + services (that duplicated the
    # composition logic and drifted from CLI/dev/API wiring).
    bundle = build_bundle(backend="sqlite", database=database, fts=fts, vectors=vectors)
    server = SeverianMCPServer(service=bundle.service, gateway=bundle.gateway)
    return server, bundle.repository, bundle.lexical, bundle.vector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m severian.mcp",
        description="Severian MCP stdio server.",
    )
    parser.add_argument("--database", type=Path, required=True, help="SQLite canonical store path.")
    parser.add_argument("--fts", type=Path, required=True, help="FTS5 index database path.")
    parser.add_argument("--vectors", type=Path, required=True, help="Vector index database path.")
    args = parser.parse_args(argv)

    server, repository, lexical, vector = _build_server(
        database=args.database,
        fts=args.fts,
        vectors=args.vectors,
    )
    try:
        server.run_stdio()
    except KeyboardInterrupt:
        pass
    finally:
        vector.close()
        lexical.close()
        repository.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
