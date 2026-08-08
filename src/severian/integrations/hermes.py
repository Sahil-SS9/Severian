"""Hermes compatibility adapter for Severian.

This module implements :class:`SeverianMemoryProvider`, a Hermes
``MemoryProvider`` that bridges Severian's canonical memory engine
into a Hermes agent lifecycle. It is dependency-optional: if the
``agent.memory_provider.MemoryProvider`` base class is importable, the
provider subclasses it; otherwise a standalone ABC is used so that
Severian remains fully independent of Hermes.

Design rules:

- **Explicit scope only.** The provider fails closed when the
  five-dimensional scope cannot be established. It never guesses a
  tenant, profile, collection, agent or session.
- **Injected service.** All reads/writes go through the injected
  :class:`MemoryService`. The provider never touches the repository
  or indexes directly.
- **No resource ownership.** The provider's ``shutdown`` (and
  compatibility ``teardown`` alias) releases provider-local state only;
  it never closes service-owned repository/index resources (the caller
  owns those).
- **Safe metadata.** ``sync_turn`` writes observations with
  source-cited metadata; ``prefetch`` returns source-cited bounded
  context. Tool calls validate explicit scope before routing.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from severian.application.derivation import DerivationGateway
from severian.application.service import MemoryService
from severian.domain.errors import NotFoundError
from severian.domain.models import Scope

_SCOPE_DIMS = ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")


class _ProviderScope(BaseModel):
    """Scope validator used internally by the provider."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    profile_id: str
    collection_id: str
    agent_id: str
    session_id: str

    @field_validator(*_SCOPE_DIMS)
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("scope dimensions must not be empty")
        return value

    def to_domain(self) -> Scope:
        return Scope(
            tenant_id=self.tenant_id,
            profile_id=self.profile_id,
            collection_id=self.collection_id,
            agent_id=self.agent_id,
            session_id=self.session_id,
        )


class _FallbackMemoryProvider(ABC):
    """Independent fallback for installations without Hermes available."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs: Any) -> None: ...

    @abstractmethod
    def get_tool_schemas(self) -> list[dict[str, Any]]: ...


# -- optional Hermes dependency -------------------------------------------

try:  # pragma: no cover - import guard
    from agent.memory_provider import (  # type: ignore[import-not-found,unused-ignore]
        MemoryProvider as _HermesMemoryProvider,
    )
except ImportError:  # pragma: no cover
    _HermesMemoryProvider = _FallbackMemoryProvider


class ScopeNotEstablishedError(RuntimeError):
    """Raised when five-dimensional scope cannot be established."""


def _encode_tool_result(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str, allow_nan=False)


def _resolve_embedding() -> Any:
    """Select the embedding provider from ``SEVERIAN_EMBEDDING``.

    Delegates to the single central resolver (zero-debt plan 2.2) — no
    environment parsing and NO silent hash substitution here. Granite
    load failure surfaces visibly at encode time (never swapped for
    hash vectors under a semantic identity).
    """
    from severian.infrastructure.embedding_resolver import embedding_from_env

    return embedding_from_env()


def _build_default_service_from_env() -> MemoryService:
    """Build a service bundle from environment configuration.

    P1-4: enables zero-arg provider construction by the real Hermes
    loader. ``SEVERIAN_DSN`` selects PostgreSQL; otherwise SQLite
    storage is used with ``SEVERIAN_STORAGE`` (default: a temp dir).
    ``SEVERIAN_EMBEDDING=granite`` selects the Granite R2 semantic
    embedding (fallback-safe). The returned service is owned by the
    caller (the provider), which must call ``shutdown()`` to release
    bundle resources.
    """
    import os
    import tempfile

    from severian.composition import build_bundle

    embedding = _resolve_embedding()
    dsn = os.environ.get("SEVERIAN_DSN", "").strip()
    if dsn:
        bundle = build_bundle(backend="postgresql", dsn=dsn, embedding=embedding)
    else:
        storage = os.environ.get("SEVERIAN_STORAGE", "").strip()
        if storage:
            root = Path(storage)
        else:
            root = Path(tempfile.mkdtemp(prefix="severian-provider-"))
        bundle = build_bundle(
            backend="sqlite",
            database=root / "severian.db",
            fts=root / "severian.fts",
            vectors=root / "severian.vec",
            embedding=embedding,
        )
    # Keep the bundle reachable so shutdown() can release it.
    provider_bundles.append(bundle)
    return bundle.service


# Module-level registry of bundles created for zero-arg providers, so
# shutdown() can close resources when the provider is torn down.
provider_bundles: list[Any] = []


class SeverianMemoryProvider(_HermesMemoryProvider):  # type: ignore[misc]
    """Hermes ``MemoryProvider`` backed by Severian's memory engine.

    The provider is constructed with an injected :class:`MemoryService`
    and an optional :class:`DerivationGateway`. Scope is not stored at
    construction; it is established by ``initialize`` and re-validated
    on every operation. Hermes tool calls must carry an explicit scope
    matching that active scope.

    If the real ``agent.memory_provider.MemoryProvider`` is not
    importable, the class falls back to the independent ABC above
    and remains fully usable as a plain Python object.
    """

    PROVIDER_NAME = "severian"
    PROVIDER_VERSION = "0.3.0"

    def __init__(
        self,
        service: MemoryService | None = None,
        gateway: DerivationGateway | None = None,
        *,
        max_context_chars: int = 8_000,
        principal: Scope | None = None,
    ) -> None:
        # P1-4: the real Hermes loader instantiates providers with a
        # zero-arg constructor (``attr()``) or via ``register(ctx)``.
        # When no service is injected, the provider builds its own
        # bundle from environment configuration (SEVERIAN_DSN for
        # PostgreSQL, SEVERIAN_STORAGE for SQLite) through the shared
        # composition root, and owns that bundle's lifecycle.
        self._owns_service = service is None
        if service is None:
            service = _build_default_service_from_env()
        self._service = service
        self._gateway = gateway
        self._scope: Scope | None = None
        self._session_id: str | None = None
        self._max_context_chars = max_context_chars
        # SEC-01: construct-time principal binding. When set, the
        # provider's scope is fixed to the principal's tenant/profile;
        # initialize() and tool calls cannot switch them.
        self._principal = principal
        if principal is not None:
            self._scope = principal

    # -- lifecycle --------------------------------------------------------

    @property
    def name(self) -> str:
        return self.PROVIDER_NAME

    def is_available(self) -> bool:
        """True when the underlying service is healthy."""
        try:
            report = self._service.health()
        except Exception:
            return False
        return report.ready

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Establish the five-dimensional scope for this session.

        Required kwargs: ``tenant_id``, ``profile_id``, ``collection_id``,
        ``agent_id``. ``session_id`` is taken from the argument (not
        the scope) unless also present in kwargs. If any dimension is
        missing or blank, the provider fails closed: no scope is
        stored and subsequent operations raise.

        SEC-01: when a principal is bound at construction, the
        tenant/profile MUST match it. A mismatch fails closed.
        """
        scope_kwargs = {
            "tenant_id": kwargs.get("tenant_id", ""),
            "profile_id": kwargs.get("profile_id", ""),
            "collection_id": kwargs.get("collection_id", ""),
            "agent_id": kwargs.get("agent_id", ""),
            "session_id": session_id,
        }
        try:
            scope = _ProviderScope.model_validate(scope_kwargs)
        except ValidationError as exc:
            # Fail closed: clear any existing scope.
            self._scope = None
            self._session_id = None
            raise ScopeNotEstablishedError(
                "cannot establish five-dimensional scope: all dimensions required and non-empty"
            ) from exc
        new_scope = scope.to_domain()
        # SEC-01: enforce principal binding.
        if self._principal is not None:
            if (
                new_scope.tenant_id != self._principal.tenant_id
                or new_scope.profile_id != self._principal.profile_id
            ):
                self._scope = None
                self._session_id = None
                raise ScopeNotEstablishedError(
                    f"scope outside principal: tenant/profile must be "
                    f"{self._principal.tenant_id}/{self._principal.profile_id}"
                )
        self._scope = new_scope
        self._session_id = session_id

    def shutdown(self) -> None:
        """Release provider-local state.

        Closes an internally-built service bundle (zero-arg construction
        path) but never closes an injected service: those resources are
        owned by the caller.
        """
        self._scope = None
        self._session_id = None
        if self._owns_service:
            # Close the bundle that built this service. The registry
            # maps bundles in construction order; the service identity
            # disambiguates when several providers share a process.
            for idx, bundle in enumerate(provider_bundles):
                if bundle.service is self._service:
                    bundle.close()
                    del provider_bundles[idx]
                    break

    def teardown(self) -> None:
        """Compatibility alias for Hermes versions exposing ``teardown``."""
        self.shutdown()

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        """Switch the active session scope.

        Re-establishes scope with the new session id. Other scope
        dimensions are taken from kwargs; if any are missing, the
        provider fails closed. The lifecycle metadata is accepted for
        compatibility but never used to infer scope.
        """
        del parent_session_id, reset, rewound
        self.initialize(session_id=new_session_id, **kwargs)

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Record a completed delegation in the established parent scope.

        The delegatee and child session are provenance metadata, not
        replacements for the parent's tenant/profile/collection/agent
        scope. The active five-dimensional scope must already exist.
        """
        scope = self._require_scope()
        if not isinstance(task, str) or not task.strip():
            raise ValueError("delegation task must not be blank")
        if not isinstance(result, str) or not result.strip():
            raise ValueError("delegation result must not be blank")
        source_value = kwargs.get("source", {})
        if not isinstance(source_value, Mapping):
            raise ValueError("source must be an object")
        source = dict(source_value)
        source.setdefault("provider", self.PROVIDER_NAME)
        source.setdefault("provider_version", self.PROVIDER_VERSION)
        source.setdefault("event", "delegation")
        if child_session_id:
            source.setdefault("child_session_id", child_session_id)
        delegatee = kwargs.get("delegatee")
        if isinstance(delegatee, str) and delegatee.strip():
            source.setdefault("delegatee", delegatee)
        self._service.ingest_observation(
            scope,
            content=f"delegated task: {task.strip()}\ndelegated result: {result.strip()}",
            source=source,
        )

    # -- prompt + context -------------------------------------------------

    def system_prompt_block(self) -> str:
        """Return a system-prompt block describing the memory provider."""
        return (
            f"[{self.PROVIDER_NAME} v{self.PROVIDER_VERSION}] "
            "Severian memory engine is active. Use the provided memory "
            "tools to ingest observations and search the memory store. "
            "All operations are scoped to the established session."
        )

    def prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> str:
        """Search and return source-cited bounded context.

        The context is assembled from search hits using the service's
        context assembler. ``limit`` (default 10) bounds the result
        count; ``derive`` (default False) controls whether derivation
        runs before search.
        """
        scope = self._require_scope()
        if session_id and session_id != scope.session_id:
            raise ScopeNotEstablishedError("prefetch session does not match active scope")
        if not isinstance(query, str) or not query.strip():
            return ""
        try:
            limit = self._validated_limit(kwargs.get("limit", 10))
        except ValueError as exc:
            raise ValueError("invalid prefetch limit") from exc
        derive_value = kwargs.get("derive", False)
        if not isinstance(derive_value, bool):
            raise ValueError("invalid prefetch derive flag")
        derive = derive_value
        hits = self._service.search(
            scope,
            query=query,
            limit=limit,
            derive=derive,
            gateway=self._gateway,
        )
        if not hits:
            return ""
        return self._service.assemble_context(scope, hits, max_chars=self._max_context_chars)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str = "",
        *,
        session_id: str = "",
        messages: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> None:
        """Write an observation for the current turn.

        ``source`` metadata (kwargs or default) is attached to the
        observation. The metadata always includes the provider name
        and a ``turn`` marker if provided.
        """
        scope = self._require_scope()
        if session_id and session_id != scope.session_id:
            raise ScopeNotEstablishedError("sync session does not match active scope")
        if not isinstance(user_content, str) or not user_content.strip():
            raise ValueError("user content must not be blank")
        if not isinstance(assistant_content, str):
            raise ValueError("assistant content must be text")
        source_value = kwargs.get("source", {})
        if not isinstance(source_value, Mapping):
            raise ValueError("source must be an object")
        source = dict(source_value)
        # Ensure safe, self-describing source metadata.
        source.setdefault("provider", self.PROVIDER_NAME)
        source.setdefault("provider_version", self.PROVIDER_VERSION)
        if "turn" in kwargs and "turn" not in source:
            source["turn"] = kwargs["turn"]
        if session_id:
            source.setdefault("session_id", session_id)
        content = user_content.strip()
        if assistant_content.strip():
            content = f"user: {content}\nassistant: {assistant_content.strip()}"
        if messages is not None:
            source.setdefault("message_count", len(messages))
        self._service.ingest_observation(scope, content=content, source=source)

    # -- tools ------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Return JSON schemas for the memory tools exposed to the agent."""
        scope_properties = {
            dimension: {"type": "string", "minLength": 1}
            for dimension in _SCOPE_DIMS
        }
        scope_required = list(_SCOPE_DIMS)

        def schema(
            properties: dict[str, Any],
            required: list[str],
        ) -> dict[str, Any]:
            return {
                "type": "object",
                "properties": {**scope_properties, **properties},
                "required": [*scope_required, *required],
                "additionalProperties": False,
            }

        return [
            {
                "name": "severian_search",
                "description": "Search the Severian memory store within the active scope.",
                "parameters": schema(
                    {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 200},
                    },
                    ["query"],
                ),
            },
            {
                "name": "severian_ingest",
                "description": "Ingest an observation into the Severian memory store within the active scope.",
                "parameters": schema(
                    {
                        "content": {"type": "string", "minLength": 1},
                        "source": {"type": "object", "default": {}},
                    },
                    ["content"],
                ),
            },
            {
                "name": "severian_get_observation",
                "description": "Fetch an observation by id within the active scope.",
                "parameters": schema(
                    {"observation_id": {"type": "string", "minLength": 1}},
                    ["observation_id"],
                ),
            },
            {
                "name": "severian_search_shared",
                "description": (
                    "Read across a set of agent identities sharing the caller's "
                    "team/task profile or collection scope (the governed shared seam). "
                    "Queries each listed agent's exact scope independently and merges "
                    "results deterministically. No wildcard: only the caller's own scope "
                    "and the explicitly listed agent_ids are touched. Use for team/task "
                    "shared memory reads."
                ),
                "parameters": schema(
                    {
                        "agent_ids": {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                            "description": "Agent identities whose memory to share into this read.",
                        },
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 200},
                    },
                    ["agent_ids", "query"],
                ),
            },
        ]

    def handle_tool_call(
        self,
        tool_name: str,
        args: Mapping[str, Any],
        **kwargs: Any,
    ) -> str:
        """Validate scope and route a tool call to the service.

        Tool arguments must carry an explicit scope. It must match the
        established provider scope exactly; this prevents cross-scope
        access through crafted arguments or stale session state.
        """
        del kwargs
        scope = self._require_scope()
        if not isinstance(args, Mapping):
            return _encode_tool_result({"error": "invalid arguments"})
        arguments = dict(args)

        scope_result = self._validate_tool_scope(arguments, scope)
        if scope_result is not None:
            return _encode_tool_result(scope_result)

        if tool_name == "severian_search":
            if set(arguments) - {*_SCOPE_DIMS, "query", "limit"}:
                return _encode_tool_result({"error": "invalid arguments"})
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return _encode_tool_result({"error": "query must not be empty"})
            try:
                limit = self._validated_limit(arguments.get("limit", 10))
            except ValueError:
                return _encode_tool_result({"error": "invalid limit"})
            hits = self._service.search(
                scope,
                query=query,
                limit=limit,
                derive=False,
                gateway=self._gateway,
            )
            return _encode_tool_result({
                "hits": [
                    {
                        "memory_id": hit.memory_id,
                        "content": hit.content,
                        "source_observation_ids": list(hit.source_observation_ids),
                        "fused_score": hit.fused_score,
                    }
                    for hit in hits
                ]
            })

        if tool_name == "severian_ingest":
            if set(arguments) - {*_SCOPE_DIMS, "content", "source"}:
                return _encode_tool_result({"error": "invalid arguments"})
            content = arguments.get("content")
            if not isinstance(content, str) or not content.strip():
                return _encode_tool_result({"error": "content must not be empty"})
            source_value = arguments.get("source", {})
            if not isinstance(source_value, Mapping):
                return _encode_tool_result({"error": "source must be an object"})
            source = dict(source_value)
            source.setdefault("provider", self.PROVIDER_NAME)
            view = self._service.ingest_observation(scope, content=content, source=source)
            return _encode_tool_result({
                "observation_id": view.observation_id,
                "content": view.content,
                "content_hash": view.content_hash,
                "job_id": view.job_id,
            })

        if tool_name == "severian_get_observation":
            if set(arguments) - {*_SCOPE_DIMS, "observation_id"}:
                return _encode_tool_result({"error": "invalid arguments"})
            observation_id = arguments.get("observation_id")
            if not isinstance(observation_id, str) or not observation_id.strip():
                return _encode_tool_result({"error": "observation_id must not be empty"})
            try:
                observation = self._service.get_observation(scope, observation_id)
            except NotFoundError:
                return _encode_tool_result({"error": "observation not found in scope"})
            return _encode_tool_result({
                "observation_id": observation.id,
                "content": observation.content,
                "content_hash": observation.content_hash,
                "created_at": observation.created_at.isoformat(),
            })

        if tool_name == "severian_search_shared":
            if set(arguments) - {*_SCOPE_DIMS, "agent_ids", "query", "limit"}:
                return _encode_tool_result({"error": "invalid arguments"})
            agent_ids = arguments.get("agent_ids")
            query = arguments.get("query")
            if not isinstance(agent_ids, list) or not agent_ids:
                return _encode_tool_result({"error": "agent_ids must be a non-empty list"})
            if not all(isinstance(a, str) and a.strip() for a in agent_ids):
                return _encode_tool_result({"error": "agent_ids must be non-empty strings"})
            if not isinstance(query, str) or not query.strip():
                return _encode_tool_result({"error": "query must not be empty"})
            try:
                limit = self._validated_limit(arguments.get("limit", 10))
            except ValueError:
                return _encode_tool_result({"error": "invalid limit"})
            # The tool args carry the caller's explicit scope (validated above
            # against the active scope). search_shared reuses that scope as the
            # base and swaps only agent_id per listed identity — preserving the
            # isolation boundary. No wildcard, no cross-tenant access.
            base_scope = _ProviderScope.model_validate(
                {dimension: arguments[dimension] for dimension in _SCOPE_DIMS}
            ).to_domain()
            shared_hits = self._service._retrieval.search_shared(
                base_scope,
                agent_ids=agent_ids,
                query=query,
                limit=limit,
            )
            return _encode_tool_result({
                "hits": [
                    {
                        "memory_id": hit.memory.id,
                        "content": hit.memory.content,
                        "source_observation_ids": list(hit.source_observation_ids),
                        "fused_score": hit.fused_score,
                    }
                    for hit in shared_hits
                ]
            })

        return _encode_tool_result({"error": "unknown tool"})

    # -- internals --------------------------------------------------------

    def _validate_tool_scope(
        self,
        arguments: dict[str, Any],
        active_scope: Scope,
    ) -> dict[str, Any] | None:
        if any(dimension not in arguments for dimension in _SCOPE_DIMS):
            return {"error": "scope is required"}
        try:
            candidate = _ProviderScope.model_validate(
                {dimension: arguments[dimension] for dimension in _SCOPE_DIMS}
            ).to_domain()
        except ValidationError:
            return {"error": "invalid scope"}
        # SEC-01: when a principal is bound, only the security-sensitive
        # dimensions (tenant/profile) must match the principal; the
        # non-security dimensions (collection/agent/session) remain
        # client-selectable. Without a principal, the candidate must
        # exactly match the active scope (existing behaviour).
        if self._principal is not None:
            if (
                candidate.tenant_id != self._principal.tenant_id
                or candidate.profile_id != self._principal.profile_id
            ):
                return {"error": "scope outside principal"}
            # Update the active scope to reflect the selected
            # collection/agent/session from the tool args.
            self._scope = candidate
            return None
        if candidate != active_scope:
            return {"error": "scope mismatch"}
        return None

    @staticmethod
    def _validated_limit(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 200:
            raise ValueError("limit must be an integer between 1 and 200")
        return value

    def _require_scope(self) -> Scope:
        if self._scope is None:
            raise ScopeNotEstablishedError(
                "scope not established: call initialize with explicit scope dimensions first"
            )
        return self._scope


def register(ctx: Any) -> None:
    """Hermes plugin entry point (P1-4).

    The real Hermes memory-provider loader (``load_memory_provider``)
    looks for a ``register(ctx)`` function in the plugin module and
    calls it with a context exposing ``register_memory_provider``. This
    entry constructs a provider with the default environment wiring and
    hands it to the loader, so ``memory.provider: severian`` in
    config.yaml activates Severian without any injected service.
    """
    provider = SeverianMemoryProvider()
    register_provider = getattr(ctx, "register_memory_provider", None)
    if register_provider is None:
        raise TypeError("register(ctx) requires ctx.register_memory_provider")
    register_provider(provider)


__all__ = [
    "ScopeNotEstablishedError",
    "SeverianMemoryProvider",
    "register",
]
