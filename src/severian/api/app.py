"""FastAPI application factory.

The API is intentionally thin: it validates request shapes, maps the
validated payload to the canonical application service, and returns
Pydantic response models. There is no direct repository, projection
or index access in this layer.

``create_app`` is the single composition root used by both production
(``uvicorn severian.api.app:app``) and tests (``TestClient(create_app(...))``).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Path, Request, status
from fastapi.responses import JSONResponse

from severian.api.schemas import (
    HealthResponse,
    IngestRequest,
    IngestResponse,
    MemoryResponse,
    ObservationResponse,
    ScopeModel,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from severian.application.derivation import DerivationGateway
from severian.application.service import MemoryService
from severian.auth.principal_resolver import PrincipalResolver
from severian.domain.errors import ForbiddenError, NotFoundError
from severian.domain.models import Scope

# Constants kept here so URL paths and tests share the same source of
# truth. Versioned under ``/api/v1`` per Phase 3 plan.
API_PREFIX = "/api/v1"
OBSERVATIONS_PATH = f"{API_PREFIX}/observations"
MEMORIES_PATH = f"{API_PREFIX}/memories"
SEARCH_PATH = f"{API_PREFIX}/search"


GatewayFactory = Callable[[], DerivationGateway]


def _scope_from_model(model: ScopeModel) -> Scope:
    return Scope(
        tenant_id=model.tenant_id,
        profile_id=model.profile_id,
        collection_id=model.collection_id,
        agent_id=model.agent_id,
        session_id=model.session_id,
    )


def _bind_principal(requested: ScopeModel, principal: Scope) -> Scope:
    """Derive the effective scope from the authenticated principal.

    P0-6: the security-sensitive dimensions (tenant/profile) are NOT
    client-selectable when a principal is bound. A request that claims
    a different tenant or profile than the principal is an
    authorization violation (403) — it is NOT a validation error, so
    callers cannot probe for cross-tenant existence via 422 vs 403.
    """
    if requested.tenant_id != principal.tenant_id or requested.profile_id != principal.profile_id:
        raise ForbiddenError("requested scope is outside the authenticated principal")
    return Scope(
        tenant_id=principal.tenant_id,
        profile_id=principal.profile_id,
        collection_id=requested.collection_id,
        agent_id=requested.agent_id,
        session_id=requested.session_id,
    )


def _view_to_observation_response(observation: Any) -> ObservationResponse:
    return ObservationResponse(
        observation_id=observation.id,
        content=observation.content,
        content_hash=observation.content_hash,
        scope=ScopeModel(
            tenant_id=observation.scope.tenant_id,
            profile_id=observation.scope.profile_id,
            collection_id=observation.scope.collection_id,
            agent_id=observation.scope.agent_id,
            session_id=observation.scope.session_id,
        ),
        source=dict(observation.source),
        created_at=observation.created_at.isoformat(),
    )


def _hit_to_response(hit: Any) -> SearchHit:
    return SearchHit(
        memory_id=hit.memory_id,
        content=hit.content,
        source_observation_ids=list(hit.source_observation_ids),
        lexical_score=hit.lexical_score,
        vector_score=hit.vector_score,
        fused_score=hit.fused_score,
        scope=ScopeModel(
            tenant_id=hit.scope.tenant_id,
            profile_id=hit.scope.profile_id,
            collection_id=hit.scope.collection_id,
            agent_id=hit.scope.agent_id,
            session_id=hit.scope.session_id,
        ),
    )


def create_app(
    *,
    service: MemoryService,
    gateway: GatewayFactory | DerivationGateway,
    principal: Scope | None = None,
    principal_resolver: PrincipalResolver | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to a ``MemoryService`` instance.

    ``gateway`` may be either a callable returning a fresh gateway per
    request (preferred — derivation may hold per-request state) or a
    fixed gateway instance used for every request (suitable for
    deterministic local fixtures).

    ``principal`` optionally binds a fixed authenticated scope. When
    set, the security-sensitive dimensions (``tenant_id`` and
    ``profile_id``) are derived server-side from the principal and any
    request body/token that disagrees with them is rejected with 403 —
    scope becomes authorization, not a client-supplied selector. When
    unset (local/dev mode), the client must supply a fully-populated
    scope as before.

    ``principal_resolver`` optionally enables per-request API-key
    authentication (SEC-01). When set, each request must carry a
    valid ``Authorization: Bearer <key>`` header; the resolved
    principal overrides the static ``principal`` (if any). Fail-closed:
    unauthenticated requests are rejected with 401. Health endpoints
    (``/healthz``, ``/readyz``) are exempt.
    """

    def _get_gateway() -> DerivationGateway:
        if callable(gateway) and not hasattr(gateway, "derive"):
            return gateway()
        return gateway  # type: ignore[return-value]

    app = FastAPI(
        title="Severian Memory API",
        version="0.1.0",
        description="Versioned HTTP API for the canonical memory platform.",
    )
    app.state.service = service

    def get_service() -> MemoryService:
        return service

    def get_gateway() -> DerivationGateway:
        return _get_gateway()

    # SEC-01: when no principal_resolver is configured (dev/loopback mode),
    # use a no-op dependency that returns None so FastAPI's parameter
    # analysis treats ``payload`` as the body. When a resolver is
    # configured, it is a callable that returns the authenticated Scope
    # (or raises 401).
    def _no_auth() -> Scope | None:
        return None

    auth_dependency = Depends(principal_resolver) if principal_resolver is not None else Depends(_no_auth)

    def resolve_scope(scope_model: ScopeModel, request_principal: Scope | None = None) -> Scope:
        """Resolve the effective scope for a request.

        With a bound principal, tenant/profile are derived server-side
        and any disagreement is a 403. Without one, the client-supplied
        scope is used as-is (local/dev mode).

        ``request_principal`` (from the auth resolver) takes precedence
        over the static ``principal`` — it is the per-request authenticated
        identity.
        """
        effective = request_principal if request_principal is not None else principal
        if effective is not None:
            return _bind_principal(scope_model, effective)
        return _scope_from_model(scope_model)

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        # Liveness: always 200 while the process serves requests.
        return HealthResponse(ready=True, components={})

    @app.get("/readyz", response_model=HealthResponse)
    def readyz(svc: MemoryService = Depends(get_service)) -> HealthResponse:
        # Readiness: dependency gate. P1-2: a service whose
        # dependencies are down must NOT report ready — callers (load
        # balancers, orchestrators) get 503 so traffic is routed away.
        report = svc.health()
        response = HealthResponse(ready=report.ready, components=report.components)
        if not report.ready:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "ready": False,
                    "components": report.components,
                    "type": "not_ready",
                },
            )
        return response

    @app.post(
        OBSERVATIONS_PATH,
        response_model=IngestResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def ingest(
        payload: IngestRequest,
        svc: MemoryService = Depends(get_service),
        request_principal: Scope | None = auth_dependency,
    ) -> IngestResponse:
        try:
            view = svc.ingest_observation(
                resolve_scope(payload.scope, request_principal),
                content=payload.content,
                source=payload.source,
            )
        except ForbiddenError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            # Pydantic surface for client errors — never leak internals.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        return IngestResponse(
            observation_id=view.observation_id,
            content=view.content,
            content_hash=view.content_hash,
            scope=ScopeModel(
                tenant_id=view.scope.tenant_id,
                profile_id=view.scope.profile_id,
                collection_id=view.scope.collection_id,
                agent_id=view.scope.agent_id,
                session_id=view.scope.session_id,
            ),
            source=dict(view.source),
            job_id=view.job_id,
            job_kind=view.job_kind,
        )

    @app.get(
        f"{OBSERVATIONS_PATH}/{{observation_id}}",
        response_model=ObservationResponse,
    )
    def get_observation(
        observation_id: str = Path(..., min_length=1),
        tenant_id: str = "",
        profile_id: str = "",
        collection_id: str = "",
        agent_id: str = "",
        session_id: str = "",
        svc: MemoryService = Depends(get_service),
        request_principal: Scope | None = auth_dependency,
    ) -> ObservationResponse:
        # P1-4: ScopeModel construction lives INSIDE the try so pydantic
        # validation failures surface as 422, not 500. Missing/invalid
        # scope dimensions are client errors, never server errors.
        try:
            scope = ScopeModel(
                tenant_id=tenant_id,
                profile_id=profile_id,
                collection_id=collection_id,
                agent_id=agent_id,
                session_id=session_id,
            )
            scope_obj = resolve_scope(scope, request_principal)
        except ForbiddenError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        try:
            observation = svc.get_observation(scope_obj, observation_id)
        except NotFoundError as exc:
            # 404 is correct: the request was well-formed but the scope
            # cannot see the requested observation. The body MUST NOT
            # disclose whether the ID exists in another scope.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="observation not found in scope",
            ) from exc
        return _view_to_observation_response(observation)

    @app.get(
        f"{MEMORIES_PATH}/{{memory_id}}",
        response_model=MemoryResponse,
    )
    def get_memory(
        memory_id: str = Path(..., min_length=1),
        tenant_id: str = "",
        profile_id: str = "",
        collection_id: str = "",
        agent_id: str = "",
        session_id: str = "",
        svc: MemoryService = Depends(get_service),
        request_principal: Scope | None = auth_dependency,
    ) -> MemoryResponse:
        # P1-4: memory-detail endpoint. Search returns memory ids (both
        # observation-derived and derived memories); the dashboard needs
        # to open any hit. Scope construction lives INSIDE the try so
        # validation failures are 422, not 500.
        try:
            scope = ScopeModel(
                tenant_id=tenant_id,
                profile_id=profile_id,
                collection_id=collection_id,
                agent_id=agent_id,
                session_id=session_id,
            )
            scope_obj = resolve_scope(scope, request_principal)
        except ForbiddenError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        try:
            memory = svc.get_memory(scope_obj, memory_id)
        except NotFoundError as exc:
            # Same discipline as observations: a well-formed request for
            # an id the scope cannot see is 404; the body never
            # discloses cross-scope existence.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="memory not found in scope",
            ) from exc
        return MemoryResponse(
            memory_id=memory.id,
            content=memory.content,
            kind=memory.kind,
            source_observation_ids=list(memory.source_observation_ids),
            scope=ScopeModel.from_domain(memory.scope),
            created_at=str(memory.created_at),
        )

    @app.post(SEARCH_PATH, response_model=SearchResponse)
    def search(
        payload: SearchRequest,
        gw: DerivationGateway = Depends(get_gateway),
        svc: MemoryService = Depends(get_service),
        request_principal: Scope | None = auth_dependency,
    ) -> SearchResponse:
        try:
            scope = resolve_scope(payload.scope, request_principal)
        except ForbiddenError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=str(exc),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        try:
            hits = svc.search(
                scope,
                query=payload.query,
                limit=payload.limit,
                derive=payload.derive,
                gateway=gw,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        return SearchResponse(hits=[_hit_to_response(hit) for hit in hits])

    @app.exception_handler(Exception)
    async def safe_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # HTTPException (404/403/422/503) must pass through with its
        # intended status; only unexpected exceptions become 500.
        # Defence-in-depth: never leak stack traces or canonical
        # payloads. QA gate SEC-03 requires no sensitive content in
        # error responses.
        if isinstance(exc, HTTPException):
            detail = exc.detail if isinstance(exc.detail, (str, dict, list)) else "error"
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": detail, "type": "http_error"},
            )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal error", "type": "server_error"},
        )

    return app


__all__ = [
    "API_PREFIX",
    "OBSERVATIONS_PATH",
    "MEMORIES_PATH",
    "SEARCH_PATH",
    "create_app",
]
