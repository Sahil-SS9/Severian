"""``python -m severian.mcp`` — stdio MCP server entry point.

Composes the canonical on-disk wiring (SQLite repository + FTS5 +
vector index) once, wires the MCP server, and runs the stdio loop.
All resources are closed on exit. Writes go exclusively through the
service; this entry point never duplicates writes.

Security boundary: stdio MCP cannot perform interactive authentication.
The launch therefore pins a tenant/profile principal, or requires an
explicit trusted-single-tenant opt-out. Collection, agent and session
remain caller-selected; only tenant and profile are bound.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from severian.composition import build_bundle
from severian.infrastructure.fts import FTS5Index
from severian.infrastructure.sqlite import SQLiteRepository
from severian.infrastructure.vector import LocalVectorIndex
from severian.mcp.server import MCPPrincipal, SeverianMCPServer

TENANT_ENV = "SEVERIAN_MCP_TENANT_ID"
PROFILE_ENV = "SEVERIAN_MCP_PROFILE_ID"

_REFUSAL = (
    "MCP stdio requires a pinned principal: pass --tenant-id and --profile-id "
    f"(or set {TENANT_ENV} and {PROFILE_ENV}), or explicitly opt out with "
    "--trusted-single-tenant for a trusted single-tenant deployment."
)
_PARTIAL = (
    "MCP stdio principal is incomplete: provide both --tenant-id and --profile-id "
    f"(or both {TENANT_ENV} and {PROFILE_ENV}); a partial pair is refused. "
    "Use --trusted-single-tenant only when no principal is intended."
)


class _LocalHashingGateway:
    """Deterministic local derivation gateway (no network)."""

    model_id: str = "local/mcp-hasher"
    prompt_version: str = "mcp-v1"
    is_cloud: bool = False

    def derive(self, content: str, *, policy: dict[str, object]) -> list[dict[str, object]]:
        return [{"kind": "fact", "content": content.strip(), "confidence": 0.5}]


def resolve_principal(
    tenant_id: str | None,
    profile_id: str | None,
    *,
    trusted_single_tenant: bool,
    environ: Mapping[str, str] | None = None,
) -> MCPPrincipal | None:
    """Resolve the launch principal, failing closed on a missing or partial pair.

    Flags win over environment values for the dimension they supply. A
    supplied principal always binds, even when the trusted opt-out is also
    present; the opt-out only permits the absence of a principal.
    """
    source = os.environ if environ is None else environ
    tenant = tenant_id if tenant_id is not None else source.get(TENANT_ENV)
    profile = profile_id if profile_id is not None else source.get(PROFILE_ENV)
    tenant = tenant.strip() if isinstance(tenant, str) else ""
    profile = profile.strip() if isinstance(profile, str) else ""

    if tenant and profile:
        return MCPPrincipal(tenant_id=tenant, profile_id=profile)
    if tenant or profile:
        raise ValueError(_PARTIAL)
    if trusted_single_tenant:
        return None
    raise ValueError(_REFUSAL)


def _build_server(
    *,
    database: Path,
    fts: Path,
    vectors: Path,
    principal: MCPPrincipal | None = None,
) -> tuple[SeverianMCPServer, SQLiteRepository, FTS5Index, LocalVectorIndex]:
    # ARCH-01: use the SINGLE composition root — the MCP runner must not
    # hand-wire repository + indexes + services (that duplicated the
    # composition logic and drifted from CLI/dev/API wiring).
    bundle = build_bundle(backend="sqlite", database=database, fts=fts, vectors=vectors)
    server = SeverianMCPServer(service=bundle.service, gateway=bundle.gateway, principal=principal)
    return server, bundle.repository, bundle.lexical, bundle.vector


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m severian.mcp",
        description="Severian MCP stdio server.",
    )
    parser.add_argument("--database", type=Path, required=True, help="SQLite canonical store path.")
    parser.add_argument("--fts", type=Path, required=True, help="FTS5 index database path.")
    parser.add_argument("--vectors", type=Path, required=True, help="Vector index database path.")
    parser.add_argument(
        "--tenant-id",
        default=None,
        help=f"Pin the tenant for this launch (or set {TENANT_ENV}).",
    )
    parser.add_argument(
        "--profile-id",
        default=None,
        help=f"Pin the profile for this launch (or set {PROFILE_ENV}).",
    )
    parser.add_argument(
        "--trusted-single-tenant",
        action="store_true",
        help=(
            "Trusted-context opt-out: allow an unbound launch where tool callers choose "
            "tenant/profile. Never use on a shared or untrusted host."
        ),
    )
    args = parser.parse_args(argv)

    try:
        principal = resolve_principal(
            args.tenant_id,
            args.profile_id,
            trusted_single_tenant=args.trusted_single_tenant,
        )
    except ValueError as exc:
        parser.error(str(exc))
        raise AssertionError("parser.error always exits") from exc

    if principal is None:
        print(
            "warning: --trusted-single-tenant disables tenant/profile pinning; "
            "tool callers choose scope. Trusted-context use only.",
            file=sys.stderr,
        )

    server, repository, lexical, vector = _build_server(
        database=args.database,
        fts=args.fts,
        vectors=args.vectors,
        principal=principal,
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
