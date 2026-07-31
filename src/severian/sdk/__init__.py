"""Python SDK for the Severian HTTP API.

The SDK is intentionally a transport layer. All scope dimensions are
required arguments — there is no implicit scope. Adapters that wrap
this SDK may add their own context propagation but must not silently
inject scope.
"""
from __future__ import annotations

from severian.sdk.client import (
    IngestRequest,
    ObservationScope,
    SearchRequest,
    SeverianClient,
)

__all__ = ["SeverianClient", "IngestRequest", "SearchRequest", "ObservationScope"]
