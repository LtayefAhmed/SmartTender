"""The single, normalised search vocabulary.

Every entry point speaks this model: the UI's advanced search form, the
``POST /scrape`` body, a stored schedule's payload, and the dashboard's
listing filter. Connectors translate it into whatever their portal expects
using the ``filter_mapping`` block of their YAML.

That indirection is what lets the UI offer the full filter set against portals
that support only a subset: anything a portal cannot express as a query
parameter is applied client-side after normalisation, so the user always gets
the result they asked for — just with a different amount of work behind it.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.enums import ProcurementType, TenderStatus

__all__ = ["FilterApplication", "TenderFilters"]


class TenderFilters(BaseModel):
    """Advanced search criteria, portal-agnostic."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # --- free text ---------------------------------------------------------
    keywords: list[str] = Field(
        default_factory=list,
        description="Terms ANDed against title and description unless `keywords_any` is set.",
    )
    keywords_any: bool = Field(
        default=False, description="Match any keyword instead of all of them."
    )
    excluded_keywords: list[str] = Field(default_factory=list)

    # --- geography ---------------------------------------------------------
    countries: list[str] = Field(default_factory=list)
    locations: list[str] = Field(
        default_factory=list, description="Region, governorate or city of performance."
    )

    # --- buyer -------------------------------------------------------------
    organizations: list[str] = Field(default_factory=list, description="Buying entities.")
    ministries: list[str] = Field(default_factory=list)
    funding_organizations: list[str] = Field(
        default_factory=list, description="Donors/financiers (BAD, BEI, World Bank...)."
    )

    # --- classification ----------------------------------------------------
    procurement_types: list[ProcurementType] = Field(default_factory=list)
    procurement_categories: list[str] = Field(
        default_factory=list, description="Works / supplies / services, portal-specific."
    )
    sectors: list[str] = Field(default_factory=list)
    cpv_codes: list[str] = Field(
        default_factory=list,
        description="Common Procurement Vocabulary codes; prefixes are accepted (e.g. '72').",
    )
    document_types: list[str] = Field(default_factory=list)

    # --- dates -------------------------------------------------------------
    publication_date_from: date | None = None
    publication_date_to: date | None = None
    deadline_from: date | None = None
    deadline_to: date | None = None
    #: Convenience shorthand used heavily by schedules: "anything published in
    #: the last N days". Resolved against the clock at execution time, so a
    #: stored schedule stays correct forever.
    published_within_days: int | None = Field(default=None, ge=1, le=365)
    min_days_until_deadline: int | None = Field(default=None, ge=0, le=365)

    # --- money -------------------------------------------------------------
    budget_min: float | None = Field(default=None, ge=0)
    budget_max: float | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=8)

    # --- misc --------------------------------------------------------------
    statuses: list[TenderStatus] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    source_websites: list[str] = Field(
        default_factory=list, description="Restrict to specific connector keys."
    )

    # --- run control (not a portal filter, but part of the request) --------
    max_results_per_source: int | None = Field(default=None, ge=1, le=10_000)
    max_pages: int | None = Field(default=None, ge=1, le=500)

    @field_validator(
        "keywords",
        "excluded_keywords",
        "countries",
        "locations",
        "organizations",
        "ministries",
        "funding_organizations",
        "procurement_categories",
        "sectors",
        "cpv_codes",
        "document_types",
        "languages",
        "source_websites",
        mode="before",
    )
    @classmethod
    def _coerce_list(cls, value: Any) -> Any:
        """Accept a bare string or a comma-separated string as a single-item list.

        Query strings arrive as ``?keywords=a,b``; JSON bodies send real lists.
        Both are legitimate and both must work.
        """
        if value is None:
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        return value

    @model_validator(mode="after")
    def _check_ranges(self) -> TenderFilters:
        if (
            self.publication_date_from
            and self.publication_date_to
            and self.publication_date_from > self.publication_date_to
        ):
            raise ValueError("publication_date_from must not be after publication_date_to")
        if self.deadline_from and self.deadline_to and self.deadline_from > self.deadline_to:
            raise ValueError("deadline_from must not be after deadline_to")
        if (
            self.budget_min is not None
            and self.budget_max is not None
            and self.budget_min > self.budget_max
        ):
            raise ValueError("budget_min must not be greater than budget_max")
        return self

    # ------------------------------------------------------------------
    def resolved(self, *, now: datetime | None = None) -> TenderFilters:
        """Expand relative shorthands into absolute dates.

        Called once at the start of a run so that every connector in the job
        sees exactly the same window even if the run spans midnight.
        """
        from datetime import timedelta

        from app.core.identity import utc_now

        reference = now or utc_now()
        data = self.model_copy(deep=True)
        if data.published_within_days is not None and data.publication_date_from is None:
            data.publication_date_from = (
                reference - timedelta(days=data.published_within_days)
            ).date()
        if data.min_days_until_deadline is not None and data.deadline_from is None:
            data.deadline_from = (
                reference + timedelta(days=data.min_days_until_deadline)
            ).date()
        return data

    def is_empty(self) -> bool:
        """True when nothing narrows the search (a full sweep of the portal)."""
        ignored = {"keywords_any", "max_results_per_source", "max_pages"}
        for name, value in self.model_dump(exclude_none=True).items():
            if name in ignored:
                continue
            if value not in ([], {}, "", None):
                return False
        return True

    def as_payload(self) -> dict[str, Any]:
        """JSON-serialisable form for persistence on a job or a schedule."""
        return self.model_dump(mode="json", exclude_defaults=True, exclude_none=True)


class FilterApplication(BaseModel):
    """How much of the filter a connector could push down to the portal.

    Surfaced to the operator so it is obvious when a search was narrowed
    server-side (fast, exact) versus filtered locally after fetching (correct,
    but far more requests).
    """

    model_config = ConfigDict(extra="forbid")

    server_side: list[str] = Field(default_factory=list)
    client_side: list[str] = Field(default_factory=list)
    unsupported: list[str] = Field(default_factory=list)
