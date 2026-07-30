"""Exception translation.

Every error leaves the API in one shape, with the stable ``code`` from the
exception hierarchy. Clients branch on the code; humans read the message.

Unhandled exceptions are logged with their traceback and returned as a generic
500 that reveals nothing about internals — stack traces in a response body are
a reconnaissance gift.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.exceptions import SmartTenderError
from app.core.logging import get_logger
from app.core.security import redact
from app.schemas.common import ErrorResponse

logger = get_logger(__name__)

__all__ = ["install_exception_handlers"]


def _response(
    request: Request, status_code: int, payload: ErrorResponse
) -> JSONResponse:
    payload.request_id = getattr(request.state, "request_id", None)
    return JSONResponse(status_code=status_code, content=payload.model_dump(exclude_none=True))


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(SmartTenderError)
    async def _domain_error(request: Request, exc: SmartTenderError) -> JSONResponse:
        log = logger.error if exc.alerting else logger.warning
        log(
            "api.domain_error",
            code=exc.code,
            path=request.url.path,
            error=exc.message,
            **redact(exc.context),
        )
        return _response(
            request,
            exc.http_status,
            ErrorResponse(
                code=exc.code,
                message=exc.message,
                detail=redact(exc.context) or None,
                retryable=exc.retryable,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors: list[dict[str, Any]] = [
            {
                "field": ".".join(str(part) for part in error.get("loc", ())[1:]) or "body",
                "message": error.get("msg"),
                "type": error.get("type"),
            }
            for error in exc.errors()
        ]
        return _response(
            request,
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            ErrorResponse(
                code="request_validation_error",
                message="The request payload is not valid.",
                detail={"errors": errors},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Routers raise HTTPException with a dict detail when the *why* matters
        # to the client — which connectors were skipped, which remain
        # available. Stringifying that would destroy the only actionable part
        # of the response, so structured detail is preserved as-is.
        if isinstance(exc.detail, dict):
            detail = dict(exc.detail)
            code = str(detail.pop("code", f"http_{exc.status_code}"))
            message = str(detail.pop("message", "")) or f"HTTP {exc.status_code}"
            return _response(
                request,
                exc.status_code,
                ErrorResponse(code=code, message=message, detail=detail or None),
            )

        return _response(
            request,
            exc.status_code,
            ErrorResponse(
                code=f"http_{exc.status_code}",
                message=str(exc.detail),
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "api.unhandled_error",
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
        )
        return _response(
            request,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            ErrorResponse(
                code="internal_error",
                message="An unexpected error occurred. The incident has been logged.",
            ),
        )
