"""Health and metrics endpoints.

Three probes with genuinely different jobs, because conflating them causes
outages:

``/health/live``   Is the process alive? No dependency checks. Kubernetes
                   restarts on failure, so a liveness probe that fails when the
                   *database* is down would restart every healthy API pod
                   during a database incident and turn a degradation into a
                   full outage.
``/health/ready``  Can this instance serve traffic? Checks its dependencies.
                   Failure removes the pod from the load balancer.
``/health``        Human-readable detail for dashboards and on-call.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response, status

from app import __version__
from app.core.config import get_settings
from app.core.metrics import CONTENT_TYPE_LATEST, build_info, render_metrics
from app.db.session import check_async_connection

router = APIRouter(tags=["health"])

_STARTED_AT = time.time()


@router.get("/health/live", summary="Liveness probe")
async def live() -> dict[str, Any]:
    return {"status": "alive", "uptime_seconds": round(time.time() - _STARTED_AT, 1)}


@router.get("/health/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, Any]:
    checks = await _dependency_checks()
    # Storage and the broker are not readiness-critical: uploads still validate
    # and tenders still list without them, and rejecting all traffic would be a
    # worse outage than a degraded one.
    ready_now = checks["database"]["ok"]
    if not ready_now:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if ready_now else "not_ready", "checks": checks}


@router.get("/health", summary="Detailed health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    checks = await _dependency_checks()
    degraded = [name for name, result in checks.items() if not result["ok"]]
    return {
        "status": "healthy" if not degraded else "degraded",
        "version": __version__,
        "environment": settings.env,
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        "degraded_components": degraded,
        "checks": checks,
    }


async def _dependency_checks() -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}

    started = time.perf_counter()
    db_ok = await check_async_connection()
    results["database"] = {
        "ok": db_ok,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
    }

    started = time.perf_counter()
    try:
        import redis.asyncio as aioredis

        settings = get_settings()
        client = aioredis.from_url(
            settings.redis.broker_url, socket_timeout=2, socket_connect_timeout=2
        )
        await client.ping()
        await client.aclose()
        broker_ok = True
        error = None
    except Exception as exc:
        broker_ok, error = False, type(exc).__name__
    results["broker"] = {
        "ok": broker_ok,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "error": error,
    }

    started = time.perf_counter()
    try:
        import anyio

        from app.services.storage import get_storage

        storage_ok = await anyio.to_thread.run_sync(get_storage().health)
        error = None
    except Exception as exc:
        storage_ok, error = False, type(exc).__name__
    results["storage"] = {
        "ok": storage_ok,
        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        "error": error,
    }

    return results


@router.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
async def metrics() -> Response:
    settings = get_settings()
    build_info.labels(version=__version__, env=settings.env).set(1)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)
