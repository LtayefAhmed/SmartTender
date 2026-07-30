"""Shared response envelopes."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")

__all__ = ["AcceptedResponse", "ErrorResponse", "Page", "PaginationParams"]


class PaginationParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=25, ge=1, le=200)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


class Page(BaseModel, Generic[T]):
    """Offset-paginated collection."""

    model_config = ConfigDict(extra="forbid")

    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        return (self.total + self.page_size - 1) // self.page_size if self.page_size else 0

    @classmethod
    def build(cls, items: list[T], total: int, params: PaginationParams) -> Page[T]:
        return cls(items=items, total=total, page=params.page, page_size=params.page_size)


class ErrorResponse(BaseModel):
    """Uniform error body.

    ``code`` is the stable machine-readable identifier from the exception
    hierarchy — clients branch on it, never on the human-readable message.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    detail: dict[str, Any] | None = None
    request_id: str | None = None
    retryable: bool = False


class AcceptedResponse(BaseModel):
    """202 body: the work was queued, not performed.

    Every heavy endpoint returns one of these. The client polls the returned
    resource rather than holding a connection open — the API contract that
    makes "the pipeline never blocks" visible from the outside.
    """

    model_config = ConfigDict(extra="forbid")

    accepted: bool = True
    message: str
    job_id: str | None = None
    tender_id: str | None = None
    task_id: str | None = None
    poll_url: str | None = None
