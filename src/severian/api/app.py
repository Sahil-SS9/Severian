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
    ObservationResponse,
    ScopeModel,
    SearchHit,
    SearchRequest,
    SearchResponse,
)
from severian.application.derivation import DerivationGateway
from severian.application.service import MemoryService
from severian.domain.errors import NotFoundError
from severian.domain.models import Scope

# Constants kept here so URL paths and tests share the same source of
# truth. Versioned under ``/api/v1`` per Phase 3 plan.
API_PREFIX = "/api/v1"
OBSERVATIONS_PATH = f"{API_PREFIX}/observations"
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
) -> FastAPI:
    """Build a FastAPI app bound to a ``MemoryService`` instance.

    ``gateway`` may be either a callable returning a fresh gateway per
    request (preferred — derivation may hold per-request state) or a
    fixed gateway instance used for every request (suitable for
    deterministic local fixtures).
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

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        return HealthResponse(ready=True, components={})

    @app.get("/readyz", response_model=HealthResponse)
    def readyz(svc: MemoryService = Depends(get_service)) -> HealthResponse:
        report = svc.health()
        return HealthResponse(ready=report.ready, components=report.components)

    @app.post(
        OBSERVATIONS_PATH,
        response_model=IngestResponse,
        status_code=status.HTTP_201_CREATED,
    )
    def ingest(
        payload: IngestRequest,
        svc: MemoryService = Depends(get_service),
    ) -> IngestResponse:
        try:
            view = svc.ingest_observation(
                _scope_from_model(payload.scope),
                content=payload.content,
                source=payload.source,
            )
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
    ) -> ObservationResponse:
        scope = ScopeModel(
            tenant_id=tenant_id,
            profile_id=profile_id,
            collection_id=collection_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        try:
            scope_obj = _scope_from_model(scope)
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

    @app.post(SEARCH_PATH, response_model=SearchResponse)
    def search(
        payload: SearchRequest,
        gw: DerivationGateway = Depends(get_gateway),
        svc: MemoryService = Depends(get_service),
    ) -> SearchResponse:
        try:
            scope = _scope_from_model(payload.scope)
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
        # Defence-in-depth: never leak stack traces or canonical
        # payloads. QA gate SEC-03 requires no sensitive content in
        # error responses.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "internal error", "type": "server_error"},
        )

    return app


__all__ = [
    "API_PREFIX",
    "OBSERVATIONS_PATH",
    "SEARCH_PATH",
    "create_app",
]
