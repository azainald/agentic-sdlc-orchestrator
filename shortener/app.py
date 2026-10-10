"""HTTP layer: the FastAPI app factory and routes.

Routes only translate HTTP <-> service calls. All dependencies (store, rate
limiter, service) live on `app.state`, so each create_app() call builds a fully
isolated app, which is what the tests rely on.
"""
import hashlib
import os

from fastapi import APIRouter, Depends, FastAPI, Header, Request, Response
from fastapi.responses import RedirectResponse

from shortener.clock import Clock, system_clock
from shortener.errors import RateLimited, install_error_handlers
from shortener.ratelimit import RateLimiter
from shortener.records import UrlRecord
from shortener.schemas import (
    AnalyticsResponse,
    CreateUrlRequest,
    ErrorResponse,
    HealthResponse,
    UpdateUrlRequest,
    UrlMetadataResponse,
    UrlResponse,
    to_iso,
)
from shortener.service import UrlService
from shortener.store import UrlRepository, UrlStore

API_KEY_HEADER = "X-API-Key"
IDEMPOTENCY_HEADER = "Idempotency-Key"
IP_SALT_ENV_VAR = "SHORTENER_IP_SALT"

router = APIRouter()


# --- dependencies ------------------------------------------------------------
def get_service(request: Request) -> UrlService:
    return request.app.state.service


def client_ip(request: Request) -> str:
    # We use the socket address. X-Forwarded-For is NOT trusted because any
    # client can fake it; behind a proxy, configure uvicorn's proxy headers.
    return request.client.host if request.client else "unknown"


def rate_limit_key(request: Request) -> str:
    """X-API-Key if present (hashed so we never keep the secret), else the IP."""
    api_key = request.headers.get(API_KEY_HEADER)
    if api_key:
        return "key:" + hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    return "ip:" + client_ip(request)


def enforce_rate_limit(request: Request) -> None:
    decision = request.app.state.limiter.check(rate_limit_key(request))
    if not decision.allowed:
        raise RateLimited(
            "rate_limited",
            f"too many requests, retry in {decision.retry_after} seconds",
            headers={"Retry-After": str(decision.retry_after)},
        )


# --- response builders ---------------------------------------------------------
def url_fields(record: UrlRecord, request: Request) -> dict:
    return {
        "code": record.code,
        "short_url": f"{request.base_url}{record.code}",
        "url": record.url,
        "expires_at": to_iso(record.expires_at),
        "created_at": to_iso(record.created_at),
    }


ERROR_RESPONSES = {
    404: {"model": ErrorResponse, "description": "Unknown code"},
    410: {"model": ErrorResponse, "description": "Link deleted or expired"},
}


# --- routes ------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse, tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@router.post(
    "/api/v1/urls",
    status_code=201,
    response_model=UrlResponse,
    tags=["urls"],
    dependencies=[Depends(enforce_rate_limit)],
    responses={
        200: {"model": UrlResponse, "description": "Idempotent replay of an earlier request"},
        409: {"model": ErrorResponse, "description": "Alias taken or Idempotency-Key reused"},
        422: {"model": ErrorResponse, "description": "Invalid url, alias or ttl_seconds"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded (see Retry-After)"},
    },
)
def create_url(
    body: CreateUrlRequest,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias=IDEMPOTENCY_HEADER),
    service: UrlService = Depends(get_service),
) -> dict:
    """Create a short link. Retrying with the same Idempotency-Key and body is safe."""
    result = service.create(body.url, body.alias, body.ttl_seconds, idempotency_key)
    if not result.created:
        response.status_code = 200
    return url_fields(result.record, request)


@router.get(
    "/api/v1/urls/{code}",
    response_model=UrlMetadataResponse,
    tags=["urls"],
    responses=ERROR_RESPONSES,
)
def get_url(
    code: str, request: Request, service: UrlService = Depends(get_service)
) -> dict:
    record = service.get(code)
    return {**url_fields(record, request), "status": service.status_of(record)}


@router.patch(
    "/api/v1/urls/{code}",
    response_model=UrlMetadataResponse,
    tags=["urls"],
    responses={**ERROR_RESPONSES, 422: {"model": ErrorResponse, "description": "Invalid update"}},
)
def update_url(
    code: str,
    body: UpdateUrlRequest,
    request: Request,
    service: UrlService = Depends(get_service),
) -> dict:
    """Change the destination and/or lifetime. ttl_seconds counts from now."""
    record = service.update(code, body.model_dump(exclude_unset=True))
    return {**url_fields(record, request), "status": service.status_of(record)}


@router.delete(
    "/api/v1/urls/{code}",
    status_code=204,
    tags=["urls"],
    responses=ERROR_RESPONSES,
)
def delete_url(code: str, service: UrlService = Depends(get_service)) -> Response:
    service.delete(code)
    return Response(status_code=204)


@router.get(
    "/api/v1/urls/{code}/analytics",
    response_model=AnalyticsResponse,
    tags=["analytics"],
    responses=ERROR_RESPONSES,
)
def get_analytics(code: str, service: UrlService = Depends(get_service)) -> dict:
    return service.analytics(code)


# Must stay LAST: "/{code}" would otherwise match paths such as "/health".
@router.get(
    "/{code}",
    status_code=302,
    response_model=None,
    response_class=RedirectResponse,
    tags=["redirect"],
    responses={302: {"description": "Redirect to the destination"}, **ERROR_RESPONSES},
)
def redirect(
    code: str,
    request: Request,
    referer: str | None = Header(default=None),
    service: UrlService = Depends(get_service),
) -> RedirectResponse:
    destination = service.resolve_redirect(code, referer, client_ip(request))
    return RedirectResponse(destination, status_code=302)


# --- factory -----------------------------------------------------------------
def create_app(
    store: UrlRepository | None = None,
    limiter: RateLimiter | None = None,
    clock: Clock | None = None,
    *,
    ip_salt: str | None = None,
) -> FastAPI:
    """Build an isolated app. Anything not supplied gets a sensible default."""
    clock = clock or system_clock
    store = store if store is not None else UrlStore(clock=clock)
    limiter = limiter if limiter is not None else RateLimiter(clock=clock)
    ip_salt = ip_salt or os.environ.get(IP_SALT_ENV_VAR)

    app = FastAPI(
        title="URL Shortener",
        version="1.0.0",
        description="Create short links, redirect, expire them and see click analytics.",
    )
    app.state.store = store
    app.state.limiter = limiter
    app.state.clock = clock
    app.state.service = UrlService(store, clock=clock, ip_salt=ip_salt)

    install_error_handlers(app)
    app.include_router(router)
    return app
