"""Source health, connector inventory and operator controls."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Principal, get_session, require_principal
from app.connectors.registry import get_registry
from app.core.logging import get_logger
from app.db.models.source import Source
from app.schemas.scrape import SourceRead

logger = get_logger(__name__)
router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=list[SourceRead], summary="List sources with health")
async def list_sources(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> list[SourceRead]:
    rows = (await session.execute(select(Source).order_by(Source.key))).scalars().all()
    return [SourceRead.model_validate(row) for row in rows]


@router.get("/registry", summary="Connector inventory as declared in configuration")
async def connector_registry(
    _: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """What the platform *can* run, and why anything unavailable is not running.

    This is the endpoint to check when a source silently produces nothing: it
    distinguishes "disabled", "missing credentials" and "configuration is
    broken", which look identical from the tender list.
    """
    registry = get_registry()
    return {
        "connectors": [info.to_dict() for info in registry.describe_all()],
        "available": registry.available_keys(),
        "errors": registry.errors(),
    }


@router.get("/{key}", response_model=SourceRead, summary="Get one source")
async def get_source(
    key: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(require_principal),
) -> SourceRead:
    row = (await session.execute(select(Source).where(Source.key == key))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown source '{key}'.")
    return SourceRead.model_validate(row)


@router.post("/{key}/toggle", response_model=SourceRead, summary="Enable or disable a source")
async def toggle_source(
    key: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> SourceRead:
    """Operator kill switch for a misbehaving portal.

    Independent of the YAML ``enabled`` flag so a source can be silenced
    immediately without a deployment.
    """
    row = (await session.execute(select(Source).where(Source.key == key))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown source '{key}'.")
    row.enabled = not row.enabled
    logger.info(
        "api.source_toggled", connector=key, enabled=row.enabled, actor=principal.identity
    )
    return SourceRead.model_validate(row)


@router.post(
    "/{key}/reset-circuit",
    response_model=SourceRead,
    summary="Force the circuit breaker closed",
)
async def reset_circuit(
    key: str,
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> SourceRead:
    """The "I fixed it" button — resume calling a source before its cooldown."""
    row = (await session.execute(select(Source).where(Source.key == key))).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Unknown source '{key}'.")

    from app.connectors.http.circuit_breaker import CircuitBreaker
    from app.core.enums import CircuitState, SourceHealth

    try:
        config = get_registry().config(key)
        breaker = CircuitBreaker(key, config.http_get("circuit_breaker"))
    except Exception:
        breaker = CircuitBreaker(key, {})

    await breaker.reset()
    await breaker.close_client()

    row.circuit_state = CircuitState.CLOSED.value
    row.circuit_opened_at = None
    row.consecutive_failures = 0
    row.health = SourceHealth.UNKNOWN.value
    row.health_reason = None

    logger.info("api.circuit_reset", connector=key, actor=principal.identity)
    return SourceRead.model_validate(row)


@router.post("/sync", summary="Re-read connector configuration")
async def sync_registry(
    session: AsyncSession = Depends(get_session),
    principal: Principal = Depends(require_principal),
) -> dict[str, Any]:
    """Reload YAML from disk and reconcile the ``sources`` table.

    Lets a selector fix take effect without restarting the API or the workers.
    """
    from app.core.config import reload_yaml_configs
    from app.services.sources import sync_sources

    reload_yaml_configs()
    get_registry().reset()

    result = await session.run_sync(sync_sources)
    logger.info("api.registry_synced", actor=principal.identity, **result)
    return {**result, "connectors": get_registry().keys()}
