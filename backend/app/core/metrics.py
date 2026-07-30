"""Prometheus instrumentation.

One module owns every metric definition so that names, labels and buckets stay
consistent and a dashboard never has to guess. Metrics are registered
idempotently: importing this module twice (which happens under pytest and under
Celery's fork model) reuses the existing collector instead of raising.

Naming follows the Prometheus conventions: ``_total`` for counters, base unit
seconds for durations, no units in labels.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client import REGISTRY as DEFAULT_REGISTRY

__all__ = [
    "CONTENT_TYPE_LATEST",
    "REGISTRY",
    "active_scraping_jobs",
    "build_info",
    "circuit_breaker_state",
    "circuit_breaker_transitions_total",
    # histograms
    "connector_duration_seconds",
    "connector_health",
    "document_size_bytes",
    "duplicate_ratio",
    "duplicates_detected_total",
    "http_request_duration_seconds",
    "http_requests_total",
    "http_retries_total",
    "notifications_sent_total",
    "observe_connector_run",
    "observe_stage",
    "parsing_failures_total",
    # gauges
    "queue_size",
    "render_metrics",
    "scoring_runs_total",
    "scraper_failure_total",
    "scraper_items_found_total",
    # counters
    "scraper_success_total",
    "stage_duration_seconds",
    "storage_operations_total",
    "tender_score",
    "tenders_ingested_total",
    "tenders_processed_total",
    "tenders_rejected_total",
    "validation_failures_total",
]

REGISTRY: CollectorRegistry = DEFAULT_REGISTRY


def _metric(cls: type, name: str, documentation: str, **kwargs: Any) -> Any:
    """Create a collector, or return the existing one on re-import."""
    try:
        return cls(name, documentation, **kwargs)
    except ValueError:
        existing = getattr(REGISTRY, "_names_to_collectors", {}).get(name)
        if existing is None:  # pragma: no cover - defensive
            raise
        return existing


# ---------------------------------------------------------------------------
# Scraping / connectors
# ---------------------------------------------------------------------------
scraper_success_total = _metric(
    Counter,
    "scraper_success_total",
    "Connector runs that completed without a fatal error.",
    labelnames=("connector", "trigger"),
)

scraper_failure_total = _metric(
    Counter,
    "scraper_failure_total",
    "Connector runs that ended in a fatal error, by error class.",
    labelnames=("connector", "trigger", "error_type"),
)

scraper_items_found_total = _metric(
    Counter,
    "scraper_items_found_total",
    "Raw tender records yielded by connectors before deduplication.",
    labelnames=("connector",),
)

connector_duration_seconds = _metric(
    Histogram,
    "connector_duration_seconds",
    "Wall-clock duration of a full connector run.",
    labelnames=("connector", "trigger"),
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1200, 1800),
)

connector_health = _metric(
    Gauge,
    "connector_health",
    "Connector health: 1 healthy, 0.5 degraded, 0 failing.",
    labelnames=("connector",),
)

active_scraping_jobs = _metric(
    Gauge,
    "active_scraping_jobs",
    "Scraping jobs currently running.",
    labelnames=("trigger",),
)


# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------
http_requests_total = _metric(
    Counter,
    "http_requests_total",
    "Outbound HTTP requests issued by the fetch layer.",
    labelnames=("connector", "host", "method", "status"),
)

http_retries_total = _metric(
    Counter,
    "http_retries_total",
    "Outbound HTTP requests retried, by reason.",
    labelnames=("connector", "host", "reason"),
)

http_request_duration_seconds = _metric(
    Histogram,
    "http_request_duration_seconds",
    "Latency of a single outbound HTTP request (excluding backoff sleeps).",
    labelnames=("connector", "host"),
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60),
)

circuit_breaker_state = _metric(
    Gauge,
    "circuit_breaker_state",
    "Circuit breaker state: 0 closed, 1 half-open, 2 open.",
    labelnames=("connector",),
)

circuit_breaker_transitions_total = _metric(
    Counter,
    "circuit_breaker_transitions_total",
    "Circuit breaker state transitions.",
    labelnames=("connector", "to_state"),
)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
stage_duration_seconds = _metric(
    Histogram,
    "stage_duration_seconds",
    "Duration of a single pipeline stage.",
    labelnames=("stage",),
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

tenders_ingested_total = _metric(
    Counter,
    "tenders_ingested_total",
    "Tenders accepted into the pipeline and persisted.",
    labelnames=("connector", "entry_point"),
)

tenders_processed_total = _metric(
    Counter,
    "tenders_processed_total",
    "Tenders that reached a terminal pipeline state.",
    labelnames=("outcome",),
)

tenders_rejected_total = _metric(
    Counter,
    "tenders_rejected_total",
    "Tenders refused before ingestion, by reason.",
    labelnames=("entry_point", "reason"),
)

duplicates_detected_total = _metric(
    Counter,
    "duplicates_detected_total",
    "Duplicates caught, by the stage that caught them.",
    labelnames=("connector", "strategy"),
)

duplicate_ratio = _metric(
    Gauge,
    "duplicate_ratio",
    "Rolling share of incoming records rejected as duplicates.",
    labelnames=("connector",),
)

parsing_failures_total = _metric(
    Counter,
    "parsing_failures_total",
    "Records that could not be parsed or normalised.",
    labelnames=("connector", "error_type"),
)

validation_failures_total = _metric(
    Counter,
    "validation_failures_total",
    "Uploads rejected by the validation layer, by rule.",
    labelnames=("reason",),
)

document_size_bytes = _metric(
    Histogram,
    "document_size_bytes",
    "Size of documents accepted into storage.",
    labelnames=("entry_point",),
    buckets=(
        10_000,
        100_000,
        500_000,
        1_000_000,
        2_500_000,
        5_000_000,
        10_000_000,
        25_000_000,
    ),
)

storage_operations_total = _metric(
    Counter,
    "storage_operations_total",
    "Object storage operations.",
    labelnames=("operation", "outcome"),
)


# ---------------------------------------------------------------------------
# Scoring & notifications
# ---------------------------------------------------------------------------
scoring_runs_total = _metric(
    Counter,
    "scoring_runs_total",
    "Scoring executions, by profile version and outcome.",
    labelnames=("profile_version", "outcome"),
)

tender_score = _metric(
    Histogram,
    "tender_score",
    "Distribution of computed relevance scores.",
    labelnames=("band",),
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 0.95, 1.0),
)

notifications_sent_total = _metric(
    Counter,
    "notifications_sent_total",
    "Notifications dispatched.",
    labelnames=("channel", "outcome"),
)


# ---------------------------------------------------------------------------
# Queues & build
# ---------------------------------------------------------------------------
queue_size = _metric(
    Gauge,
    "queue_size",
    "Messages waiting in a Celery queue.",
    labelnames=("queue",),
)

build_info = _metric(
    Gauge,
    "build_info",
    "Build metadata, always 1.",
    labelnames=("version", "env"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@contextmanager
def observe_stage(stage: str) -> Iterator[None]:
    """Time a pipeline stage. Records duration on success *and* on failure."""
    started = time.perf_counter()
    try:
        yield
    finally:
        stage_duration_seconds.labels(stage=stage).observe(time.perf_counter() - started)


@contextmanager
def observe_connector_run(connector: str, trigger: str) -> Iterator[dict[str, Any]]:
    """Time a connector run and record success/failure exactly once.

    The yielded dict accepts ``items_found`` so the caller does not need to
    touch the counters itself.
    """
    result: dict[str, Any] = {"items_found": 0}
    started = time.perf_counter()
    active_scraping_jobs.labels(trigger=trigger).inc()
    try:
        yield result
    except BaseException as exc:
        scraper_failure_total.labels(
            connector=connector, trigger=trigger, error_type=type(exc).__name__
        ).inc()
        raise
    else:
        scraper_success_total.labels(connector=connector, trigger=trigger).inc()
        found = int(result.get("items_found") or 0)
        if found:
            scraper_items_found_total.labels(connector=connector).inc(found)
    finally:
        connector_duration_seconds.labels(connector=connector, trigger=trigger).observe(
            time.perf_counter() - started
        )
        active_scraping_jobs.labels(trigger=trigger).dec()


def render_metrics() -> bytes:
    """Serialise the registry in Prometheus text exposition format."""
    return generate_latest(REGISTRY)
