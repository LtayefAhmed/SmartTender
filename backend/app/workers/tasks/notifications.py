"""Notification tasks.

Creation and delivery are separate tasks on purpose. Creating the rows is
fast, transactional and must always succeed; delivering them talks to SMTP,
which is the least reliable dependency in the system. Splitting them means a
dead mail server produces retryable delivery rows rather than blocking the
pipeline stage that produced them.

The tender reaches ``COMPLETED`` as soon as its notifications are *created*.
Whether an email eventually lands is a delivery concern, not a reason to leave
a tender stuck in an intermediate state.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from sqlalchemy import select

from app.core.enums import NotificationStatus, PipelineStage, TenderPipelineState
from app.core.exceptions import NotificationError
from app.core.identity import utc_now
from app.core.logging import get_logger, log_context
from app.core.metrics import tenders_processed_total
from app.db.models.notification import Notification
from app.db.models.tender import Tender
from app.db.session import session_scope
from app.services import audit
from app.services.notifications import NotificationService
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = [
    "deliver_notification",
    "dispatch_tender_notifications",
    "flush_pending_notifications",
    "send_digests",
]


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.notifications.dispatch_tender_notifications",
    queue="notifications",
)
def dispatch_tender_notifications(self: PipelineTask, tender_id: str) -> dict[str, Any]:
    """Create notifications for every user this tender matches."""
    with log_context(tender_uuid=tender_id):
        service = NotificationService()
        notification_ids: list[str] = []

        with session_scope() as session:
            tender = session.get(Tender, uuid_module.UUID(tender_id))
            if tender is None:
                logger.warning("notification.tender_missing", tender_uuid=tender_id)
                return {"tender_id": tender_id, "status": "missing"}

            # Replay guard: an at-least-once redelivery must not double-notify.
            already = session.execute(
                select(Notification.id).where(Notification.tender_id == tender.id).limit(1)
            ).first()
            if already is not None:
                tender.pipeline_state = TenderPipelineState.COMPLETED.value
                return {"tender_id": tender_id, "status": "already_dispatched"}

            created = service.build_for_tender(session, tender)
            session.flush()
            notification_ids = [
                str(n.id) for n in created if n.status == NotificationStatus.PENDING.value
            ]

            tender.pipeline_state = TenderPipelineState.COMPLETED.value
            audit.record_event(
                session,
                "notification.dispatched",
                stage=PipelineStage.NOTIFY,
                tender_id=tender.id,
                connector=tender.source_key,
                message=f"{len(created)} notification(s) created.",
                context={"count": len(created), "band": tender.relevance_band},
            )

        for notification_id in notification_ids:
            deliver_notification.apply_async(
                kwargs={"notification_id": notification_id}, queue="notifications"
            )

        tenders_processed_total.labels(outcome="completed").inc()
        logger.info("notification.dispatched", count=len(notification_ids))
        return {"tender_id": tender_id, "created": len(notification_ids)}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.notifications.deliver_notification",
    queue="notifications",
    max_retries=4,
)
def deliver_notification(self: PipelineTask, notification_id: str) -> dict[str, Any]:
    """Deliver one notification on its channel."""
    service = NotificationService()
    try:
        with session_scope() as session:
            notification = session.get(Notification, uuid_module.UUID(notification_id))
            if notification is None:
                return {"notification_id": notification_id, "status": "missing"}
            if notification.status in {
                NotificationStatus.SENT.value,
                NotificationStatus.READ.value,
                NotificationStatus.SUPPRESSED.value,
            }:
                return {"notification_id": notification_id, "status": notification.status}

            service.deliver(session, notification)
            status = notification.status
    except NotificationError as exc:
        # Retried with backoff; when the budget is spent the row stays FAILED
        # and is visible in the admin console. The tender is unaffected.
        self.retry_or_fail(exc)
        raise

    return {"notification_id": notification_id, "status": status}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.notifications.flush_pending_notifications",
    queue="notifications",
)
def flush_pending_notifications(self: PipelineTask, limit: int = 500) -> dict[str, Any]:
    """Re-dispatch notifications left pending by an outage.

    The safety net behind the enqueue-after-commit rule: if the broker was
    unavailable when a notification was created, this periodic sweep picks it
    up instead of leaving it stranded forever.
    """
    from datetime import timedelta

    cutoff = utc_now() - timedelta(minutes=5)
    with session_scope() as session:
        stale = list(
            session.execute(
                select(Notification.id)
                .where(
                    Notification.status == NotificationStatus.PENDING.value,
                    Notification.created_at <= cutoff,
                    Notification.attempts < 3,
                )
                .limit(limit)
            )
            .scalars()
            .all()
        )

    for notification_id in stale:
        deliver_notification.apply_async(
            kwargs={"notification_id": str(notification_id)}, queue="notifications"
        )

    if stale:
        logger.info("notification.flush_dispatched", count=len(stale))
    return {"dispatched": len(stale)}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.notifications.send_digests",
    queue="notifications",
)
def send_digests(self: PipelineTask, frequency: str = "daily") -> dict[str, Any]:
    """Batch a period's notifications into one email per digest subscriber.

    Digest subscribers receive in-app notifications immediately; only the email
    is batched. Before this existed they received no email at all, which was
    indistinguishable from the platform being broken.
    """
    service = NotificationService()
    digest_ids: list[str] = []

    with session_scope() as session:
        digests = service.build_all_digests(session, frequency=frequency)
        session.flush()
        digest_ids = [str(d.id) for d in digests]

    for digest_id in digest_ids:
        deliver_notification.apply_async(
            kwargs={"notification_id": digest_id}, queue="notifications"
        )

    if digest_ids:
        logger.info("digest.dispatched", frequency=frequency, count=len(digest_ids))
    return {"frequency": frequency, "digests": len(digest_ids)}
