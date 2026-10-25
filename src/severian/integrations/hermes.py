"""Hermes compatibility adapter for Severian.

This module implements :class:`SeverianMemoryProvider`, a Hermes
``MemoryProvider`` that bridges Severian's canonical memory engine
into a Hermes agent lifecycle. It is dependency-optional: if the
``agent.memory_provider.MemoryProvider`` base class is importable, the
provider subclasses it; otherwise a standalone ABC is used so that
Severian remains fully independent of Hermes.

Design rules:

- **Never a guessed scope.** The five-dimensional scope is either given
  in full by the host or derived deterministically from the Hermes
  profile identity (``agent_identity``). Missing or blank values fail
  closed, and a partial explicit scope is never completed.
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
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from severian.application.anticipation import MAX_REMINDERS
from severian.application.derivation import DerivationGateway
from severian.application.eventtime import parse_time
from severian.application.service import SEARCH_VIEWS, MemoryService
from severian.domain.errors import ConflictError, NotFoundError
from severian.domain.models import Scope, TaskNote

logger = logging.getLogger(__name__)

_SCOPE_DIMS = ("tenant_id", "profile_id", "collection_id", "agent_id", "session_id")
_EXPLICIT_DIMS = _SCOPE_DIMS[:-1]  # all but session_id
_DEFAULT_FORGET_REASON = "user requested erasure"
_WRITE_TOOLS = frozenset({"severian_ingest", "severian_correct", "severian_forget"})
_TASK_ACTIONS = ("set", "get", "list", "clear")
_TASK_WRITES = frozenset({"set", "clear"})

# The prefetch context cap. The Hermes loader builds the provider with no arguments, so the cap is read from
# SEVERIAN_CONTEXT_CHARS (0.3.12); unset keeps the long-standing 8,000. Benchmarks (docs/benchmarks/omnimemeval-2026-10.md)
# show the cap is the main cost/quality trade-off on stores of long messages.
DEFAULT_CONTEXT_CHARS = 8_000
MIN_CONTEXT_CHARS = 1_000


def context_chars_from_env() -> int:
    """``SEVERIAN_CONTEXT_CHARS`` as a whole number >= 1,000, else the 8,000 default (a bad value is logged, never
    raised: a typo in the env file must not leave every profile without memory)."""
    raw = os.environ.get("SEVERIAN_CONTEXT_CHARS", "").strip()
    if not raw:
        return DEFAULT_CONTEXT_CHARS
    if raw.isdigit() and int(raw) >= MIN_CONTEXT_CHARS:
        return int(raw)
    logger.warning(
        "SEVERIAN_CONTEXT_CHARS=%r is not a whole number >= %d; using %d", raw, MIN_CONTEXT_CHARS, DEFAULT_CONTEXT_CHARS
    )
    return DEFAULT_CONTEXT_CHARS


def _note_entry(note: TaskNote) -> dict[str, Any]:
    return {"task": note.task, "state": note.state, "metadata": dict(note.metadata),
            "updated_at": note.created_at.isoformat()}


def _single_target(arguments: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return exactly one non-empty mutation target, or None."""
    observation = arguments.get("observation_id")
    memory = arguments.get("memory_id")
    present = [("observation", observation), ("memory", memory)]
    selected = [(kind, value.strip()) for kind, value in present if isinstance(value, str) and value.strip()]
    return selected[0] if len(selected) == 1 else None

# Hermes-derived scope (used when the host passes ``agent_identity`` instead of
# explicit dimensions). One profile-wide memory per profile: every chat of the
# profile reads and writes the same scope, and the chat id is provenance.
HERMES_DEFAULT_TENANT = "kensei"
HERMES_TENANT_ENV = "SEVERIAN_TENANT"
HERMES_PROFILE_COLLECTION = "profile"
HERMES_PROFILE_SESSION = "shared"
# Hermes ``agent_context`` values whose turns must not be written.
_NON_PRIMARY_CONTEXTS = frozenset({"cron", "subagent", "flush"})


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
    storage is used with ``SEVERIAN_STORAGE`` (default: the shared Hermes
    store, see :func:`_default_storage_root`).
    ``SEVERIAN_EMBEDDING=qwen`` selects the pinned Qwen 0.6B semantic
    embedding. ``granite`` remains available only as an explicit legacy
    migration/rollback selection; semantic load failure is never silently
    substituted with hash. The returned service is owned by the caller
    (the provider), which must call ``shutdown()`` to release bundle
    resources.
    """
    from severian.composition import build_bundle

    embedding = _resolve_embedding()
    dsn = os.environ.get("SEVERIAN_DSN", "").strip()
    if dsn:
        bundle = build_bundle(backend="postgresql", dsn=dsn, embedding=embedding)
    else:
        storage = os.environ.get("SEVERIAN_STORAGE", "").strip()
        root = Path(storage) if storage else _default_storage_root()
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def _default_storage_root() -> Path:
    """The shared Hermes store, ``<hermes root>/severian``.

    Used when ``SEVERIAN_STORAGE`` is unset. Every profile shares one store
    (``search_shared`` and team memory need that), so a profile home
    (``<root>/profiles/<name>``) resolves to its root. A throwaway temp dir
    used to be the fallback: writes then vanished without any error.
    """
    raw = os.environ.get("HERMES_HOME", "").strip()
    home = Path(raw).expanduser() if raw else Path.home() / ".hermes"
    if home.parent.name == "profiles":
        home = home.parent.parent
    return home / "severian"


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
    PROVIDER_VERSION = "0.3.11+h1h2"

    def __init__(
        self,
        service: MemoryService | None = None,
        gateway: DerivationGateway | None = None,
        *,
        max_context_chars: int | None = None,
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
        # The host's chat/session id. Equal to ``_scope.session_id`` for an
        # explicit scope; provenance only for a Hermes-derived profile scope.
        self._session_id: str | None = None
        # Step 4: reminders already shown in the current session (memory ids), so none repeats.
        self._reminded: set[str] = set()
        self._derived_scope = False
        self._writes_enabled = True
        self._platform: str | None = None
        self._max_context_chars = context_chars_from_env() if max_context_chars is None else max_context_chars
        # SEC-01: construct-time principal binding. When set, the
        # provider's scope is fixed to the principal's tenant/profile;
        # initialize() and tool calls cannot switch them.
        self._principal = principal
        if principal is not None:
            self._scope = principal
            self._session_id = principal.session_id

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

        Two contracts, never mixed:

        - **Explicit:** any of ``tenant_id``, ``profile_id``,
          ``collection_id`` or ``agent_id`` in kwargs selects it, and then
          all four are required. ``session_id`` (the argument) is the
          session dimension.
        - **Hermes-derived:** with no explicit dimension, ``agent_identity``
          (the Hermes profile name) yields one profile-wide scope:
          tenant ``SEVERIAN_TENANT`` (default ``kensei``), profile = agent =
          identity, collection ``profile``, session ``shared``. Every chat
          of the profile shares it; ``session_id`` is kept as provenance.

        Missing or blank values fail closed: no scope is stored and later
        operations raise. ``agent_context`` of ``cron``, ``subagent`` or
        ``flush`` disables automatic writes (``sync_turn``,
        ``on_delegation``); reads stay available.

        SEC-01: when a principal is bound at construction, the
        tenant/profile MUST match it. A mismatch fails closed.
        """
        derived = "agent_identity" in kwargs and kwargs.keys().isdisjoint(_EXPLICIT_DIMS)
        if derived:
            identity = kwargs.get("agent_identity")
            identity = identity.strip() if isinstance(identity, str) else ""
            tenant = os.environ.get(HERMES_TENANT_ENV, "").strip() or HERMES_DEFAULT_TENANT
            scope_kwargs = {
                "tenant_id": tenant,
                "profile_id": identity,
                "collection_id": HERMES_PROFILE_COLLECTION,
                "agent_id": identity,
                "session_id": HERMES_PROFILE_SESSION,
            }
        else:
            scope_kwargs = {
                "tenant_id": kwargs.get("tenant_id", ""),
                "profile_id": kwargs.get("profile_id", ""),
                "collection_id": kwargs.get("collection_id", ""),
                "agent_id": kwargs.get("agent_id", ""),
                "session_id": session_id,
            }
        try:
            scope = _ProviderScope.model_validate(scope_kwargs)
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError("session id must not be empty")
        except (ValidationError, ValueError) as exc:
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
        context = kwargs.get("agent_context", "primary")
        platform = kwargs.get("platform")
        self._scope = new_scope
        self._session_id = session_id
        self._reminded = set()
        self._derived_scope = derived
        self._writes_enabled = not (isinstance(context, str) and context in _NON_PRIMARY_CONTEXTS)
        self._platform = platform if isinstance(platform, str) and platform.strip() else None

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
        """Rotate only the session dimension of an already trusted scope.

        Hermes lifecycle hooks do not carry the four stable scope dimensions.
        Reusing the scope accepted by :meth:`initialize` prevents a session
        change from becoming a new, model-selectable identity binding. A
        provider that has never established a scope still fails closed.
        A Hermes-derived profile scope has no per-chat dimension: it is kept
        unchanged and only the tracked chat id moves.
        """
        del parent_session_id, reset, rewound, kwargs
        scope = self._require_scope()
        if self._derived_scope:
            if not isinstance(new_session_id, str) or not new_session_id.strip():
                self._scope = None
                self._session_id = None
                raise ScopeNotEstablishedError("cannot switch to an empty session scope")
            self._session_id = new_session_id
            return
        try:
            next_scope = _ProviderScope.model_validate(
                {
                    "tenant_id": scope.tenant_id,
                    "profile_id": scope.profile_id,
                    "collection_id": scope.collection_id,
                    "agent_id": scope.agent_id,
                    "session_id": new_session_id,
                }
            ).to_domain()
        except ValidationError as exc:
            self._scope = None
            self._session_id = None
            raise ScopeNotEstablishedError("cannot switch to an empty session scope") from exc
        self._scope = next_scope
        self._session_id = new_session_id

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
        if not self._writes_enabled:
            return
        source_value = kwargs.get("source", {})
        if not isinstance(source_value, Mapping):
            raise ValueError("source must be an object")
        source = dict(source_value)
        source.setdefault("provider", self.PROVIDER_NAME)
        source.setdefault("provider_version", self.PROVIDER_VERSION)
        source.setdefault("event", "delegation")
        self._add_session_provenance(source)
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
        """Return a system-prompt block describing the memory provider.

        Memory tools require the exact active scope as arguments, so the
        block names it once a scope is established; without it the model
        could not form a valid tool call.
        """
        block = (
            f"[{self.PROVIDER_NAME} v{self.PROVIDER_VERSION}] "
            "Severian memory engine is active. Use the provided memory "
            "tools include severian_correct when a fact is wrong and severian_forget when the user asks to forget it. "
            "Give exactly one target from a search hit (observation_id or memory_id), not both. "
            "Use severian_task_progress to save and read where you left off on a task."
        )
        if self._scope is None:
            return block
        scope_args = ", ".join(f"{dimension}={getattr(self._scope, dimension)}" for dimension in _SCOPE_DIMS)
        return f"{block} Pass these exact scope arguments to every memory tool: {scope_args}."

    def prefetch(self, query: str, *, session_id: str = "", **kwargs: Any) -> str:
        """Source-cited bounded context, plus at most two due reminders (Step 4, off by default)."""
        context = self._prefetch_context(query, session_id=session_id, **kwargs)
        reminders = self._reminders(query, kwargs)
        return f"{context}\n\n{reminders}" if context and reminders else context or reminders

    def _reminders(self, query: str, kwargs: Mapping[str, Any]) -> str:
        """Instructions-for-later that fit this turn: at most 2, each shown once per session, logged."""
        if not isinstance(query, str) or not query.strip() or self._scope is None:
            return ""
        due = self._service.due(
            self._scope,
            tool=str(kwargs.get("tool", "") or ""),
            path=str(kwargs.get("path", "") or ""),
            repo=str(kwargs.get("repo", "") or ""),
            message=query,
        )
        # Once per session: a reminder already shown is not repeated every turn (no nagging).
        fresh = [item for item in due if item.memory_id not in self._reminded][:MAX_REMINDERS]
        if not fresh:
            return ""
        for item in fresh:
            self._reminded.add(item.memory_id)
            logger.info("severian reminder shown memory=%s session=%s", item.memory_id, self._session_id)
        lines = "\n".join(f"- {item.content[:300]}" for item in fresh)
        return f"[Reminders: instructions you were given earlier that apply now.]\n{lines}"

    def _prefetch_context(self, query: str, *, session_id: str = "", **kwargs: Any) -> str:
        """Search and return source-cited bounded context.

        The context is assembled from search hits using the service's
        context assembler. ``limit`` (default 10) bounds the result
        count; ``derive`` (default False) controls whether derivation
        runs before search.
        """
        scope = self._require_scope()
        if session_id and session_id != self._session_id:
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
        bootstrap_scope = Scope(
            tenant_id=scope.tenant_id,
            profile_id=scope.profile_id,
            collection_id="bootstrap",
            agent_id=scope.agent_id,
            session_id="shared",
        )
        session_hits = self._service.search(
            scope,
            query=query,
            limit=limit,
            derive=derive,
            gateway=self._gateway,
        )
        bootstrap_hits = self._service.search(
            bootstrap_scope,
            query=query,
            limit=limit,
            derive=derive,
            gateway=self._gateway,
        )
        if not bootstrap_hits:
            return self._service.assemble_context(
                scope,
                session_hits,
                max_chars=self._max_context_chars,
                query=query,
            ) if session_hits else ""

        header = "[Profile bootstrap reference — Current system and user instructions take priority.]\n"
        session_budget = max(0, self._max_context_chars // 2)
        session_context = self._service.assemble_context(
            scope,
            session_hits,
            max_chars=session_budget,
            query=query,
        ) if session_hits else ""
        separator = "\n\n" if session_context else ""
        bootstrap_budget = self._max_context_chars - len(session_context) - len(separator) - len(header)
        if bootstrap_budget <= 0:
            return session_context[:self._max_context_chars]
        bootstrap_context = self._service.assemble_context(
            bootstrap_scope,
            bootstrap_hits,
            max_chars=bootstrap_budget,
            query=query,
        )
        if not bootstrap_context:
            return session_context
        return f"{session_context}{separator}{header}{bootstrap_context}"[:self._max_context_chars]

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
        if session_id and session_id != self._session_id:
            raise ScopeNotEstablishedError("sync session does not match active scope")
        if not isinstance(user_content, str) or not user_content.strip():
            raise ValueError("user content must not be blank")
        if not isinstance(assistant_content, str):
            raise ValueError("assistant content must be text")
        if not self._writes_enabled:
            return
        source_value = kwargs.get("source", {})
        if not isinstance(source_value, Mapping):
            raise ValueError("source must be an object")
        source = dict(source_value)
        # Ensure safe, self-describing source metadata.
        source.setdefault("provider", self.PROVIDER_NAME)
        source.setdefault("provider_version", self.PROVIDER_VERSION)
        if "turn" in kwargs and "turn" not in source:
            source["turn"] = kwargs["turn"]
        self._add_session_provenance(source)
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
                "description": (
                    "Search the Severian memory store within the active scope. view: now (default, "
                    "current facts), history (current facts plus replaced ones, marked previously) or "
                    "changes (only the facts that replaced an older one). as_of (YYYY-MM-DD or ISO time) "
                    "answers as the store stood then."
                ),
                "parameters": schema(
                    {
                        "query": {"type": "string", "minLength": 1},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 200},
                        "view": {"type": "string", "enum": ["now", "history", "changes"], "default": "now"},
                        "as_of": {"type": "string", "minLength": 8},
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
            {
                "name": "severian_correct",
                "description": (
                    "Correct a remembered fact when the user says it is wrong. Give exactly one target "
                    "from a severian_search hit: observation_id (one of its source_observation_ids; the "
                    "source is replaced by your corrected text and re-derived on the next memory pass) or "
                    "memory_id (that memory is replaced at once). The old version is no longer recalled; "
                    "the history keeps it."
                ),
                "parameters": schema(
                    {
                        "observation_id": {"type": "string", "minLength": 1},
                        "memory_id": {"type": "string", "minLength": 1},
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "description": "The corrected fact, stated in full.",
                        },
                    },
                    ["content"],
                ),
            },
            {
                "name": "severian_forget",
                "description": (
                    "Forget a remembered fact when the user asks you to. Give exactly one target from a "
                    "severian_search hit: observation_id (preferred: forgets that source and everything "
                    "derived from it) or memory_id (forgets that one memory). It is no longer recalled "
                    "anywhere; the original stays in the audit history."
                ),
                "parameters": schema(
                    {
                        "observation_id": {"type": "string", "minLength": 1},
                        "memory_id": {"type": "string", "minLength": 1},
                        "reason": {"type": "string", "minLength": 1, "default": _DEFAULT_FORGET_REASON},
                    },
                    [],
                ),
            },
            {
                "name": "severian_task_progress",
                "description": (
                    "Where you left off on a task, kept across chats and sessions. set: save the current state "
                    "of a task (replaces the previous note, which stays as history); get: read one task's "
                    "latest state (include_history for earlier ones); list: all tracked tasks, newest first; "
                    "clear: stop tracking a task. Notes are working state, not facts: search never shows them."
                ),
                "parameters": schema(
                    {
                        "action": {"type": "string", "enum": list(_TASK_ACTIONS)},
                        "task": {"type": "string", "minLength": 1, "maxLength": 200},
                        "state": {"type": "string", "minLength": 1, "maxLength": 20000},
                        "metadata": {"type": "object", "default": {}},
                        "include_history": {"type": "boolean", "default": False},
                    },
                    ["action"],
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
        if tool_name == "severian_task_progress":
            return _encode_tool_result(self._task_progress(scope, arguments))
        if tool_name in _WRITE_TOOLS and not self._writes_enabled:
            # Same contract as turn sync: cron, subagent and flush runs read only.
            return _encode_tool_result({"error": "memory writes are disabled in this context"})

        if tool_name == "severian_search":
            if set(arguments) - {*_SCOPE_DIMS, "query", "limit", "view", "as_of"}:
                return _encode_tool_result({"error": "invalid arguments"})
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip():
                return _encode_tool_result({"error": "query must not be empty"})
            try:
                limit = self._validated_limit(arguments.get("limit", 10))
            except ValueError:
                return _encode_tool_result({"error": "invalid limit"})
            view = arguments.get("view", "now")
            if view not in SEARCH_VIEWS:
                return _encode_tool_result({"error": "invalid view"})
            extra: dict[str, Any] = {}
            if "as_of" in arguments:
                as_of = parse_time(arguments["as_of"])
                if as_of is None:
                    return _encode_tool_result({"error": "invalid as_of"})
                # A bare date means the end of that day; an explicit time is used as given.
                extra["as_of"] = as_of.date() if len(str(arguments["as_of"]).strip()) <= 10 else as_of
            hits = self._service.search(
                scope,
                query=query,
                limit=limit,
                derive=False,
                gateway=self._gateway,
                view=view,
                **extra,
            )
            entries = []
            for hit in hits:
                entry: dict[str, Any] = {
                    "memory_id": hit.memory_id,
                    "content": hit.content,
                    "source_observation_ids": list(hit.source_observation_ids),
                    "fused_score": hit.fused_score,
                }
                if view != "now":
                    entry["truth_state"] = hit.truth_state
                    if hit.truth_state == "historical":
                        entry["content"] = f"(previously) {hit.content}"
                    if hit.previous:
                        entry["previously"] = list(hit.previous)
                entries.append(entry)
            return _encode_tool_result({"hits": entries})

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

        if tool_name == "severian_correct":
            if set(arguments) - {*_SCOPE_DIMS, "observation_id", "memory_id", "content"}:
                return _encode_tool_result({"error": "invalid arguments"})
            target = _single_target(arguments)
            if target is None:
                return _encode_tool_result({"error": "give exactly one of observation_id or memory_id"})
            content = arguments.get("content")
            if not isinstance(content, str) or not content.strip():
                return _encode_tool_result({"error": "content must not be empty"})
            kind, target_id = target
            try:
                if kind == "observation":
                    revised = self._service.revise_observation(scope, target_id, content.strip())
                    return _encode_tool_result(
                        {"corrected": "observation", "id": revised.id, "revision_of": target_id}
                    )
                corrected = self._service.correct_memory(
                    scope,
                    target_id,
                    content=content.strip(),
                    model_id=f"hermes-tool/{scope.agent_id}",
                    prompt_version="severian_correct",
                )
            except NotFoundError:
                return _encode_tool_result({"error": "not found in scope"})
            except ConflictError:
                return _encode_tool_result({"error": "already corrected or forgotten"})
            return _encode_tool_result({"corrected": "memory", "id": corrected.id, "revision_of": target_id})

        if tool_name == "severian_forget":
            if set(arguments) - {*_SCOPE_DIMS, "observation_id", "memory_id", "reason"}:
                return _encode_tool_result({"error": "invalid arguments"})
            target = _single_target(arguments)
            if target is None:
                return _encode_tool_result({"error": "give exactly one of observation_id or memory_id"})
            reason = arguments.get("reason", _DEFAULT_FORGET_REASON)
            if not isinstance(reason, str) or not reason.strip():
                return _encode_tool_result({"error": "reason must not be empty"})
            kind, target_id = target
            try:
                if kind == "observation":
                    self._service.erase_observation(scope, target_id, reason=reason.strip())
                else:
                    self._service.erase_memory(scope, target_id, reason=reason.strip())
            except NotFoundError:
                return _encode_tool_result({"error": "not found in scope"})
            except ConflictError:
                return _encode_tool_result({"error": "already corrected or forgotten"})
            return _encode_tool_result({"forgotten": kind, "id": target_id})

        return _encode_tool_result({"error": "unknown tool"})

    # -- internals --------------------------------------------------------

    def _task_progress(self, scope: Scope, arguments: dict[str, Any]) -> dict[str, Any]:
        if set(arguments) - {*_SCOPE_DIMS, "action", "task", "state", "metadata", "include_history"}:
            return {"error": "invalid arguments"}
        action = arguments.get("action")
        if action not in _TASK_ACTIONS:
            return {"error": "action must be one of set, get, list, clear"}
        if action in _TASK_WRITES and not self._writes_enabled:
            return {"error": "memory writes are disabled in this context"}
        if action == "list":
            return {"tasks": [_note_entry(note) for note in self._service.list_task_notes(scope)]}
        task = arguments.get("task")
        if not isinstance(task, str) or not task.strip() or len(task) > 200:
            return {"error": "task must be a non-empty name (up to 200 characters)"}
        task = task.strip()
        if action == "set":
            state, metadata = arguments.get("state"), arguments.get("metadata", {})
            if not isinstance(state, str) or not state.strip() or len(state) > 20000:
                return {"error": "state must be non-empty text (up to 20000 characters)"}
            if not isinstance(metadata, Mapping):
                return {"error": "metadata must be an object"}
            try:
                note = self._service.set_task_note(scope, task, state.strip(), dict(metadata))
            except ConflictError:
                return {"error": "task note changed at the same moment; try again"}
            return _note_entry(note)
        if action == "clear":
            if self._service.clear_task_note(scope, task):
                return {"cleared": task}
            return {"error": "no note for this task"}
        current, history = self._service.get_task_note(scope, task)
        if current is None:
            return {"error": "no note for this task"}
        entry = _note_entry(current)
        if arguments.get("include_history") is True:
            entry["history"] = [_note_entry(note) for note in history]
        return entry

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
            raise ScopeNotEstablishedError("scope not established: call initialize first")
        return self._scope

    def _add_session_provenance(self, source: dict[str, Any]) -> None:
        """Record which chat (and platform) produced a write."""
        if self._session_id:
            source.setdefault("session_id", self._session_id)
        if self._platform:
            source.setdefault("platform", self._platform)


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
