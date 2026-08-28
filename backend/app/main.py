"""FastAPI application factory.

Startup deliberately does *not* require the database, Redis or MinIO to be
reachable. An API that refuses to boot when a dependency is briefly down turns
a recoverable degradation into an outage and, worse, makes the readiness probe
useless — there is nothing left to report the degradation. Instead the app
starts, reports itself not-ready, and recovers on its own when the dependency
returns.

The one thing that *does* fail fast is a production deployment with no API keys
configured, because silently serving an unauthenticated API is not a
degradation, it is an incident.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__
from app.api.errors import install_exception_handlers
from app.api.routers import (
    admin,
    cvs,
    health,
    job_match,
    notifications,
    schedules,
    scrape,
    sources,
    tenders,
    upload,
)
from app.core.config import get_settings
from app.core.exceptions import ConfigurationError
from app.core.logging import (
    bind_context,
    clear_context,
    configure_logging,
    get_logger,
    new_request_id,
)
from app.core.metrics import build_info

logger = get_logger(__name__)

DESCRIPTION = """
Tender detection and ingestion platform (SmartTender AI — Module 1).

Three entry points converge on one pipeline:

* **`POST /scrape`** — manual, filtered scraping across selected sources.
* **`POST /upload`** — manual document upload, validated synchronously.
* **`/schedules`** — customisable recurring scraping, editable at runtime.

Every heavy endpoint returns `202 Accepted` with a resource to poll. Nothing in
this API waits on a portal, a parser or a mail server.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()

    if settings.is_production and not settings.api.api_keys:
        raise ConfigurationError(
            "Refusing to start: no API keys are configured in a production "
            "environment. Set SMARTTENDER_API__API_KEYS.",
        )

    build_info.labels(version=__version__, env=settings.env).set(1)

    # Load the connector registry eagerly so a broken config surfaces in the
    # startup log rather than in the first user's scrape request.
    try:
        from app.connectors.registry import get_registry

        registry = get_registry()
        registry.load(force=True)
        logger.info(
            "api.startup",
            version=__version__,
            environment=settings.env,
            connectors=registry.keys(),
            connector_errors=registry.errors(),
        )
    except Exception as exc:
        logger.error("api.registry_load_failed", error=str(exc))

    yield

    from app.db.session import dispose_async_engine

    await dispose_async_engine()
    logger.info("api.shutdown")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind logging context, and time every request."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = request_id
        bind_context(request_id=request_id, correlation_id=request_id)

        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration = time.perf_counter() - started
            logger.exception(
                "http.request_failed",
                method=request.method,
                path=request.url.path,
                duration_seconds=round(duration, 4),
            )
            raise
        finally:
            clear_context()

        duration = time.perf_counter() - started
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration:.4f}"

        # Health probes fire constantly; logging them buries everything else.
        if not request.url.path.startswith(("/health", "/metrics")):
            logger.info(
                "http.request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_seconds=round(duration, 4),
                request_id=request_id,
            )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline response hardening.

    The API serves JSON only, so a restrictive CSP costs nothing here and
    protects the Swagger UI from becoming an XSS vector.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )
        if get_settings().is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    app = FastAPI(
        title=settings.api.title,
        description=DESCRIPTION,
        version=__version__,
        root_path=settings.api.root_path,
        lifespan=lifespan,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json",
    )

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-Response-Time"],
    )

    install_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(scrape.router)
    app.include_router(upload.router)
    app.include_router(cvs.router)
    app.include_router(job_match.router)
    app.include_router(tenders.router)
    app.include_router(schedules.router)
    app.include_router(sources.router)
    app.include_router(notifications.router)
    app.include_router(admin.router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        return {
            "service": settings.service_name,
            "version": __version__,
            "docs": "/docs",
            "health": "/health",
        }

    return app


app = create_app()
