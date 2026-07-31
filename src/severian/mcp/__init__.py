"""MCP adapter for Severian.

Implements a Model Context Protocol server over JSON-RPC 2.0 using
only the Python standard library and Pydantic. The optional MCP SDK
is not required: this package speaks the wire protocol directly so
Severian remains dependency-light.

The adapter exposes three tools -- ``severian_ingest_observation``,
``severian_search`` and ``severian_get_observation`` -- each requiring
the full five-dimensional scope (``tenant_id``, ``profile_id``,
``collection_id``, ``agent_id``, ``session_id``) on every call. Missing
or blank scope is rejected before the canonical service is touched.
"""
from __future__ import annotations

from severian.mcp.server import SeverianMCPServer

__all__ = ["SeverianMCPServer"]
