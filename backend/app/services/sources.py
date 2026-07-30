"""Source registry synchronisation and health tracking.

Bridges the declarative world (YAML in ``config/connectors/``) and the
operational one (rows in ``sources``). ``sync_sources`` runs at worker and API
startup and is idempotent: it creates rows for new connectors, refreshes their
descriptive fields, and leaves every accumulated statistic untouched.

Health is derived, never hand-set. That matters because the interesting failure
is not "the portal returned 500" — that is loud and obvious. It is the portal
that returns HTTP 200 with an empty listing because a selector broke, which
looks like success everywhere except in ``consecutive_empty_runs``.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.connectors.models import ConnectorOutcome
from app.connectors.registry import get_registry
from app.core.enums import SourceHealth
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.core.metrics import connector_health, duplicate_ratio
from app.db.models.source import Source

logger = get_logger(__name__)

__all__ = ["apply_outcome", "get_or_create_source", "recompute_health", "sync_sources"]

_HEALTH_VALUE = {
    SourceHealth.HEALTHY: 1.0,
    SourceHealth.DEGRADED: 0.5,
    SourceHealth.FAILING: 0.0,
    SourceHealth.DISABLED: 0.0,
    SourceHealth.CREDENTIALS_MISSING: 0.0,
    SourceHealth.UNKNOWN: 0.5,
}


def _health_gauge(health: str | None) -> float:
    """Map a stored health string to its metric value.

    Tolerates ``None`` and unrecognised values. A freshly constructed ``Source``
    has ``health=None`` until it is flushed — column defaults are applied by the
    database, not the constructor — and ``SourceHealth(None)`` raises, which
    previously took the whole startup source-sync down with it.
    """
    if not health:
        return _HEALTH_VALUE[SourceHealth.UNKNOWN]
    try:
        return _HEALTH_VALUE.get(SourceHealth(health), 0.5)
    except ValueError:
        return 0.5


def sync_sources(session: Session) -> dict[str, int]:
    """Reconcile the ``sources`` table with the connector registry."""
    registry = get_registry()
    registry.load(force=True)

    existing = {
        source.key: source
        for source in session.execute(select(Source)).scalars().all()
    }

    created = updated = 0
    for info in registry.describe_all():
        config = registry.config(info.key)
        source = existing.get(info.key)
        if source is None:
            source = Source(key=info.key)
            session.add(source)
            created += 1
        else:
            updated += 1

        # Descriptive fields track the YAML; operational counters do not.
        source.name = info.name
        source.base_url = info.base_url
        source.country = info.country
        source.language = config.language
        source.strategy = info.strategy
        source.requires_credentials = info.requires_credentials
        source.config_checksum = info.checksum

        if not info.available:
            source.health = (
                SourceHealth.CREDENTIALS_MISSING.value
                if info.unavailable_reason == "credentials_missing"
                else SourceHealth.DISABLED.value
            )
            source.health_reason = info.unavailable_reason
        elif source.health in (
            SourceHealth.DISABLED.value,
            SourceHealth.CREDENTIALS_MISSING.value,
        ):
            # It just became available again — clear the blocking state without
            # inventing a health verdict it has not earned yet.
            source.health = SourceHealth.UNKNOWN.value
            source.health_reason = None

        connector_health.labels(connector=info.key).set(_health_gauge(source.health))

    for key, source in existing.items():
        if key not in set(registry.keys()):
            # The config file is gone. Disable rather than delete: the tenders
            # it produced still reference it.
            source.enabled = False
            source.health = SourceHealth.DISABLED.value
            source.health_reason = "configuration_removed"
            logger.warning("source.configuration_removed", key=key)

    logger.info("sources.synced", created=created, updated=updated)
    return {"created": created, "updated": updated}


def get_or_create_source(session: Session, key: str) -> Source:
    source = session.execute(select(Source).where(Source.key == key)).scalar_one_or_none()
    if source is not None:
        return source
    try:
        info = get_registry().describe(key)
        source = Source(
            key=key,
            name=info.name,
            base_url=info.base_url,
            country=info.country,
            strategy=info.strategy,
            requires_credentials=info.requires_credentials,
            config_checksum=info.checksum,
        )
    except Exception:
        source = Source(key=key, name=key)
    session.add(source)
    session.flush()
    return source


def apply_outcome(session: Session, source: Source, outcome: ConnectorOutcome) -> None:
    """Fold one connector run into the source's running statistics and health."""
    now = utc_now()
    source.last_run_at = now
    source.last_duration_seconds = outcome.duration_seconds
    source.last_item_count = outcome.items_found

    if outcome.skipped:
        # A skip is not a data point about the portal's health.
        return

    source.total_runs = (source.total_runs or 0) + 1

    if outcome.succeeded:
        source.last_success_at = now
        source.consecutive_failures = 0
        source.consecutive_successes = (source.consecutive_successes or 0) + 1
        source.total_items_found = (source.total_items_found or 0) + outcome.items_found
        source.last_error_type = None
        source.last_error_message = None

        if outcome.items_found == 0:
            source.consecutive_empty_runs = (source.consecutive_empty_runs or 0) + 1
        else:
            source.consecutive_empty_runs = 0
    else:
        source.last_failure_at = now
        source.total_failures = (source.total_failures or 0) + 1
        source.consecutive_failures = (source.consecutive_failures or 0) + 1
        source.consecutive_successes = 0
        source.last_error_type = outcome.error_type
        source.last_error_message = (outcome.error_message or "")[:2000] or None

    recompute_health(source)

    if source.total_items_found:
        duplicate_ratio.labels(connector=source.key).set(
            (source.total_duplicates or 0) / source.total_items_found
        )


def recompute_health(source: Source, *, health_policy: dict[str, Any] | None = None) -> None:
    """Derive health from the run history."""
    if not source.enabled:
        source.health = SourceHealth.DISABLED.value
    elif source.health == SourceHealth.CREDENTIALS_MISSING.value:
        pass  # not an operational verdict; leave it alone
    elif source.consecutive_failures >= 3:
        source.health = SourceHealth.FAILING.value
        source.health_reason = (
            f"{source.consecutive_failures} consecutive failures "
            f"({source.last_error_type or 'unknown error'})"
        )
    elif source.consecutive_failures > 0:
        source.health = SourceHealth.DEGRADED.value
        source.health_reason = f"Last run failed: {source.last_error_type or 'unknown error'}"
    else:
        policy = health_policy or {}
        empty_threshold = int(policy.get("empty_run_alert_threshold") or 3)
        if (source.consecutive_empty_runs or 0) >= empty_threshold:
            # The dangerous case: HTTP 200, zero rows. Almost always a broken
            # selector rather than a genuinely empty portal.
            source.health = SourceHealth.DEGRADED.value
            source.health_reason = (
                f"{source.consecutive_empty_runs} consecutive runs returned no items — "
                "the portal markup may have changed."
            )
        else:
            source.health = SourceHealth.HEALTHY.value
            source.health_reason = None

    connector_health.labels(connector=source.key).set(_health_gauge(source.health))
