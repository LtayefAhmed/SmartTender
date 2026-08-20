"""FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import verify_api_key
from app.db.session import get_db_session
from app.schemas.common import PaginationParams

__all__ = ["Principal", "get_session", "optional_principal", "pagination", "require_principal"]


@dataclass(slots=True)
class Principal:
    """The authenticated caller.

    Deliberately minimal. Module 1 authenticates service-to-service traffic
    with an API key; the platform's user identity and RBAC live in the auth
    module and arrive here as a header the gateway has already validated.
    """

    identity: str
    is_anonymous: bool = False
    #: The organisation this caller acts for.
    #:
    #: Tenders are public notices and are shared; a CV is not. Two firms using
    #: the platform must never see each other's candidates, and that boundary
    #: is far cheaper to draw now — with three rows in the table — than to
    #: retrofit across every query later.
    #:
    #: Defaults rather than being required: today there is one organisation,
    #: and a deployment that has not thought about tenancy should behave
    #: correctly rather than reject every request.
    tenant: str = "default"


async def get_session() -> AsyncIterator[AsyncSession]:
    async for session in get_db_session():
        yield session


def pagination(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=200),
) -> PaginationParams:
    return PaginationParams(page=page, page_size=page_size)


async def require_principal(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
) -> Principal:
    """Authenticate the caller, or reject with 401.

    With no keys configured the check is disabled entirely — acceptable in
    local development, and refused outright in production by the startup
    validation in ``main``.
    """
    settings = get_settings()
    accepted = settings.api.api_keys
    # Normalised because it becomes a stored partition key: "Inetum" and
    # "inetum " must not create two invisible halves of one corpus.
    tenant = (x_tenant_id or settings.api.default_tenant).strip().lower() or "default"

    if not accepted:
        return Principal(
            identity=x_user_id or "anonymous", is_anonymous=True, tenant=tenant
        )

    if verify_api_key(x_api_key, accepted):
        return Principal(identity=x_user_id or "service", tenant=tenant)

    if settings.api.allow_anonymous:
        return Principal(identity="anonymous", is_anonymous=True, tenant=tenant)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid X-API-Key header is required.",
        headers={"WWW-Authenticate": "ApiKey"},
    )


async def optional_principal(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Principal:
    return Principal(identity=x_user_id or "anonymous", is_anonymous=x_user_id is None)


CurrentPrincipal = Depends(require_principal)
