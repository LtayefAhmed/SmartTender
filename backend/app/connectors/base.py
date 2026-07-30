"""The connector contract and its failure-proof orchestration.

A connector author implements four small methods and nothing else:

    fetch()      yield pages           — where the bytes come from
    parse()      page  -> records      — where the strings are
    validate()   record -> ok/raise    — is this record usable
    normalize()  record -> tender      — canonical vocabulary

``run()`` is a template method implemented once, here, and shared by every
connector. It owns the concerns that must never be left to a per-source
implementation: the circuit breaker, the time budget, per-item error isolation,
transport statistics, metrics, and — most importantly — the guarantee that it
**returns an outcome instead of raising**.

That last property is the whole isolation invariant in one sentence. A
connector that times out, crashes, or returns nonsense produces a
``ConnectorOutcome`` with ``succeeded=False``; the job that launched it counts
the failure and carries on with every other source.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.connectors.config import ConnectorConfig
from app.connectors.http.circuit_breaker import CircuitBreaker
from app.connectors.http.client import ResilientHttpClient
from app.connectors.models import (
    ConnectorOutcome,
    FetchedPage,
    ItemFailure,
    NormalizedTender,
    RawRecord,
)
from app.core.config import get_settings
from app.core.enums import JobTrigger
from app.core.exceptions import (
    CircuitOpenError,
    ConnectorError,
    CredentialsMissingError,
    NormalizationError,
    ParsingError,
    SmartTenderError,
    ValidationError,
)
from app.core.identity import canonicalize_url
from app.core.logging import get_logger, log_context
from app.core.metrics import observe_connector_run, parsing_failures_total
from app.core.security import redact_url
from app.schemas.filters import FilterApplication, TenderFilters

logger = get_logger(__name__)

__all__ = ["BaseConnector", "ConnectorContext"]

#: Hard ceiling on how many per-item failures we record verbatim. Beyond this
#: the count keeps rising but the payloads stop, so a catastrophically broken
#: portal cannot produce a multi-megabyte error blob in the database.
MAX_RECORDED_ITEM_FAILURES = 50


@dataclass(slots=True)
class ConnectorContext:
    """Ambient information for one run. Never mutated by connector code."""

    job_id: str | None = None
    run_id: str | None = None
    trigger: JobTrigger = JobTrigger.MANUAL
    #: Wall-clock ceiling for the whole run. ``run()`` stops cleanly and
    #: returns what it has rather than being killed mid-write.
    deadline_seconds: float = 1800.0
    max_items: int | None = None
    max_pages: int | None = None
    #: Set for the fixture connector and for local integration tests only.
    allow_private_hosts: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class BaseConnector(ABC):
    """Base class for every source connector."""

    def __init__(self, config: ConnectorConfig, context: ConnectorContext | None = None) -> None:
        self.config = config
        self.context = context or ConnectorContext()
        self.key = config.key
        self.log = get_logger(f"connector.{config.key}")

        self.http: ResilientHttpClient | None = None
        self.breaker = CircuitBreaker(config.key, config.http_get("circuit_breaker"))
        self._started_at = 0.0
        self._pages_fetched = 0
        self._filter_application = FilterApplication()

    # ------------------------------------------------------------------
    # Required interface
    # ------------------------------------------------------------------
    @abstractmethod
    def fetch(self, filters: TenderFilters) -> AsyncIterator[FetchedPage]:
        """Yield raw pages for the given filters.

        Implementations must be async generators, must respect
        ``self.remaining_seconds`` and ``self.context.max_pages``, and must not
        interpret the bytes they yield — that is ``parse``'s job. Keeping the
        two apart is what lets the parser be unit-tested against a fixture with
        no network at all.
        """
        raise NotImplementedError

    @abstractmethod
    def parse(self, page: FetchedPage) -> list[RawRecord]:
        """Extract untyped records from one page. Pure function, no I/O."""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, record: RawRecord) -> NormalizedTender:
        """Convert a raw record into the canonical model."""
        raise NotImplementedError

    def validate(self, record: RawRecord) -> None:
        """Reject records that cannot produce a usable tender.

        The default enforces the connector's ``required_fields``; override to
        add source-specific rules. Raise ``ValidationError`` — the caller
        records it against the item and moves to the next one.
        """
        for field_name in self.config.required_fields:
            if not record.value(field_name):
                raise ValidationError(
                    f"Record is missing the required field '{field_name}'.",
                    field=field_name,
                    context={"connector": self.key, "url": redact_url(record.source_url)},
                )

    # ------------------------------------------------------------------
    # Optional hooks
    # ------------------------------------------------------------------
    async def setup(self) -> None:
        """Open resources. Called once, before ``fetch``."""

    async def teardown(self) -> None:
        """Release resources. Always called, even after a failure."""

    async def authenticate(self) -> None:
        """Establish a session. Called after ``setup`` when credentials exist."""

    async def enrich(self, record: RawRecord) -> RawRecord:
        """Optionally fetch a detail page to complete a listing record.

        Runs per item and is allowed to fail: a record that cannot be enriched
        is still ingested with whatever the listing gave us. Partial data beats
        no data.
        """
        return record

    def matches_filters(self, tender: NormalizedTender, filters: TenderFilters) -> bool:
        """Client-side filtering for criteria the portal could not express.

        The default implementation covers the criteria that are meaningful on a
        normalised tender; connectors rarely need to override it.
        """
        return _client_side_match(tender, filters)

    # ------------------------------------------------------------------
    # Helpers available to subclasses
    # ------------------------------------------------------------------
    @property
    def remaining_seconds(self) -> float:
        elapsed = time.perf_counter() - self._started_at
        return max(0.0, self.context.deadline_seconds - elapsed)

    @property
    def out_of_time(self) -> bool:
        return self.remaining_seconds <= 0

    @property
    def max_pages(self) -> int:
        configured = int(self.config.pagination.get("max_pages") or 20)
        if self.context.max_pages:
            return min(configured, self.context.max_pages)
        return configured

    def endpoint(self, name: str, **params: Any) -> str:
        """Resolve a named endpoint from the config, substituting placeholders."""
        template = self.config.endpoints.get(name)
        if not template:
            from app.core.exceptions import ConfigurationError

            raise ConfigurationError(
                f"Connector '{self.key}' has no endpoint named '{name}'.",
                context={"connector": self.key},
            )
        return template.format(**params) if params else template

    def note_page(self) -> None:
        self._pages_fetched += 1

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    async def run(self, filters: TenderFilters | None = None) -> ConnectorOutcome:
        """Execute the connector end to end. Never raises."""
        filters = (filters or TenderFilters()).resolved()
        outcome = ConnectorOutcome(connector_key=self.key)
        self._started_at = time.perf_counter()
        trigger = self.context.trigger.value

        with log_context(
            connector=self.key,
            job_id=self.context.job_id,
            task_id=self.context.run_id,
        ):
            skip_reason = self._preflight_skip_reason()
            if skip_reason:
                outcome.skipped = True
                outcome.succeeded = True
                outcome.skip_reason = skip_reason
                self.log.info("connector.skipped", reason=skip_reason)
                return outcome

            try:
                await self.breaker.check()
            except CircuitOpenError as exc:
                outcome.skipped = True
                outcome.succeeded = True
                outcome.skip_reason = "circuit_open"
                outcome.error_context = exc.context
                self.log.warning("connector.skipped", reason="circuit_open", **exc.context)
                return outcome

            try:
                with observe_connector_run(self.key, trigger) as observed:
                    await self._execute(filters, outcome)
                    observed["items_found"] = outcome.items_found
                await self.breaker.record_success()

                # A run that fetched pages and extracted nothing from any of
                # them is the one failure this pipeline cannot afford to report
                # as success — it looks identical to a quiet week at the portal.
                # The per-page guard selectors catch most of it; this catches
                # the rest, and stops short of failing the run because an empty
                # portal is legitimate.
                if outcome.pages_fetched and not outcome.records_parsed:
                    self.log.warning(
                        "connector.no_records_parsed",
                        pages_fetched=outcome.pages_fetched,
                        hint="Selectors may be stale; verify against the live page.",
                    )

            except asyncio.CancelledError:
                # A cancelled run is a shutdown or a hard timeout, not a source
                # failure. Do not blame the portal for it.
                outcome.succeeded = False
                outcome.error_type = "CancelledError"
                outcome.error_message = "Run was cancelled before completion."
                self.log.warning("connector.cancelled", items_so_far=outcome.items_found)

            except SmartTenderError as exc:
                outcome.succeeded = False
                outcome.error_type = type(exc).__name__
                outcome.error_message = exc.message
                outcome.error_context = exc.context
                if not isinstance(exc, CredentialsMissingError):
                    await self.breaker.record_failure()
                log_method = self.log.error if exc.alerting else self.log.warning
                log_method(
                    "connector.failed",
                    error_type=type(exc).__name__,
                    error=exc.message,
                    alerting=exc.alerting,
                    items_recovered=outcome.items_found,
                    **exc.context,
                )

            except Exception as exc:
                # The catch-all is the point: an unforeseen bug in one
                # connector must degrade that source and nothing else.
                outcome.succeeded = False
                outcome.error_type = type(exc).__name__
                outcome.error_message = str(exc)[:1000]
                await self.breaker.record_failure()
                self.log.exception(
                    "connector.unexpected_error",
                    error_type=type(exc).__name__,
                    items_recovered=outcome.items_found,
                )

            finally:
                try:
                    await self.teardown()
                except Exception:
                    self.log.warning("connector.teardown_failed", exc_info=True)
                if self.http is not None:
                    outcome.http_requests = self.http.stats.requests
                    outcome.http_retries = self.http.stats.retries
                    outcome.bytes_downloaded = self.http.stats.bytes_downloaded
                    await self.http.aclose()
                await self.breaker.close_client()
                outcome.pages_fetched = self._pages_fetched
                outcome.duration_seconds = time.perf_counter() - self._started_at
                outcome.filter_application = self._filter_application

            self.log.info("connector.finished", **outcome.to_summary())
            return outcome

    # ------------------------------------------------------------------
    def _preflight_skip_reason(self) -> str | None:
        """Reasons to not even attempt the run. All of them are normal."""
        settings = get_settings()
        if not self.config.enabled:
            return "disabled"
        environments = self.config.environments
        if environments and settings.env not in environments:
            return f"not_enabled_in_env:{settings.env}"
        if self.config.requires_credentials and not self.config.has_credentials():
            return "credentials_missing"
        return None

    async def _execute(self, filters: TenderFilters, outcome: ConnectorOutcome) -> None:
        """The happy path, with per-item isolation."""
        await self.setup()
        if self.config.requires_credentials:
            await self.authenticate()

        seen_keys: set[str] = set()
        max_items = self.context.max_items or filters.max_results_per_source

        async for page in self.fetch(filters):
            if self.out_of_time:
                self.log.warning(
                    "connector.deadline_reached",
                    pages_fetched=self._pages_fetched,
                    items_found=outcome.items_found,
                )
                break

            try:
                records = self.parse(page)
            except ParsingError as exc:
                # A page we cannot parse is recorded and skipped. One bad page
                # does not invalidate the pages already collected — except for
                # a broken guard selector, which means the whole markup moved
                # and continuing would just produce silent zeros.
                parsing_failures_total.labels(
                    connector=self.key, error_type=type(exc).__name__
                ).inc()
                outcome.item_failures.append(
                    ItemFailure(
                        url=page.url,
                        error_type=type(exc).__name__,
                        message=exc.message,
                        context=exc.context,
                    )
                )
                if exc.alerting:
                    raise
                continue

            outcome.records_parsed += len(records)

            for record in records:
                if max_items and outcome.items_found >= max_items:
                    self.log.info("connector.item_limit_reached", limit=max_items)
                    return

                try:
                    tender = await self._process_record(record)
                except (ValidationError, NormalizationError, ParsingError, ConnectorError) as exc:
                    self._record_item_failure(outcome, record, exc)
                    continue
                except Exception as exc:
                    self._record_item_failure(outcome, record, exc)
                    continue

                if tender is None:
                    continue

                # In-run dedup: portals routinely repeat a notice across pages,
                # and paying for the downstream pipeline twice is pure waste.
                dedup_key = tender.canonical_url or tender.external_id or tender.title
                if dedup_key in seen_keys:
                    outcome.items_duplicate_in_run += 1
                    continue
                seen_keys.add(dedup_key)

                if not self.matches_filters(tender, filters):
                    outcome.items_filtered_out += 1
                    continue

                outcome.tenders.append(tender)

    async def _process_record(self, record: RawRecord) -> NormalizedTender | None:
        """enrich -> validate -> normalize, for one record."""
        try:
            record = await self.enrich(record)
        except SmartTenderError as exc:
            # Enrichment is best-effort by design.
            self.log.info(
                "connector.enrich_failed",
                url=redact_url(record.source_url),
                error_type=type(exc).__name__,
                error=exc.message,
            )

        self.validate(record)

        try:
            tender = self.normalize(record)
        except (ValidationError, NormalizationError):
            raise
        except Exception as exc:
            raise NormalizationError(
                "Record could not be normalised into the canonical model.",
                connector=self.key,
                url=redact_url(record.source_url),
                context={"error": str(exc)[:300]},
                cause=exc,
            ) from exc

        if tender.source_url and not tender.canonical_url:
            strip = (
                (self.config.get("dedup") or {}).get("strip_query_params")
                or _default_strip_params()
            )
            tender.canonical_url = canonicalize_url(tender.source_url, strip_params=strip)
        return tender

    def _record_item_failure(
        self, outcome: ConnectorOutcome, record: RawRecord, exc: BaseException
    ) -> None:
        parsing_failures_total.labels(connector=self.key, error_type=type(exc).__name__).inc()
        if len(outcome.item_failures) < MAX_RECORDED_ITEM_FAILURES:
            message = exc.message if isinstance(exc, SmartTenderError) else str(exc)
            outcome.item_failures.append(
                ItemFailure(
                    url=record.source_url,
                    error_type=type(exc).__name__,
                    message=message[:500],
                    context=exc.context if isinstance(exc, SmartTenderError) else {},
                )
            )
        self.log.debug(
            "connector.item_failed",
            url=redact_url(record.source_url),
            error_type=type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Client-side filtering
# ---------------------------------------------------------------------------
def _default_strip_params() -> list[str]:
    from app.core.config import load_yaml_config

    try:
        canonical = load_yaml_config("dedup").get("canonical_url", {})
        return list(canonical.get("strip_query_params", []))
    except Exception:
        return []


def _contains_any(haystack: str | None, needles: list[str]) -> bool:
    if not needles:
        return True
    if not haystack:
        return False
    lowered = haystack.lower()
    return any(needle.lower() in lowered for needle in needles)


def _client_side_match(tender: NormalizedTender, filters: TenderFilters) -> bool:
    """Apply filter criteria that the portal could not express itself."""
    blob = " ".join(
        part
        for part in (
            tender.title,
            tender.description,
            tender.buyer,
            tender.sector,
            tender.category,
            tender.reference,
        )
        if part
    ).lower()

    if filters.keywords:
        needles = [k.lower() for k in filters.keywords]
        hits = [n in blob for n in needles]
        if not (any(hits) if filters.keywords_any else all(hits)):
            return False

    if filters.excluded_keywords and any(k.lower() in blob for k in filters.excluded_keywords):
        return False

    if filters.countries and not _contains_any(tender.country, filters.countries):
        return False
    if filters.locations and not _contains_any(
        f"{tender.location or ''} {tender.country or ''}", filters.locations
    ):
        return False
    if filters.organizations and not _contains_any(tender.buyer, filters.organizations):
        return False
    if filters.funding_organizations and not _contains_any(
        tender.funding_organization, filters.funding_organizations
    ):
        return False
    if filters.sectors and not _contains_any(
        f"{tender.sector or ''} {tender.category or ''}", filters.sectors
    ):
        return False
    if filters.procurement_types and tender.procurement_type not in filters.procurement_types:
        return False
    if filters.statuses and tender.status not in filters.statuses:
        return False
    if filters.languages and tender.language and tender.language not in filters.languages:
        return False

    if filters.cpv_codes and tender.cpv_codes:
        # Prefix matching: a filter on "72" must match "72200000".
        if not any(
            code.startswith(prefix.rstrip("0") or prefix)
            for code in tender.cpv_codes
            for prefix in filters.cpv_codes
        ):
            return False

    if filters.publication_date_from and tender.publication_date:
        if tender.publication_date.date() < filters.publication_date_from:
            return False
    if filters.publication_date_to and tender.publication_date:
        if tender.publication_date.date() > filters.publication_date_to:
            return False
    if filters.deadline_from and tender.deadline:
        if tender.deadline.date() < filters.deadline_from:
            return False
    if filters.deadline_to and tender.deadline:
        if tender.deadline.date() > filters.deadline_to:
            return False

    # Budget bounds only exclude when the amount is actually known: dropping
    # every tender with an unpublished budget would discard most of the market.
    if tender.estimated_budget is not None:
        amount = float(tender.estimated_budget)
        if filters.budget_min is not None and amount < filters.budget_min:
            return False
        if filters.budget_max is not None and amount > filters.budget_max:
            return False

    return True
