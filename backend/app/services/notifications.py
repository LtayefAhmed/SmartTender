"""Notification targeting and delivery.

Targeting is the hard part, not delivery. A platform that emails every user
about every tender gets muted in a week, and once muted it has destroyed the
signal it exists to provide. So a user is only notified when the tender
intersects their declared interests *and* clears their relevance floor *and*
they are under their daily cap.

Delivery is deliberately fault-isolated: notifications are persisted first and
sent second, so a dead SMTP server costs the user a delayed email, never a
missed tender. ``NotificationError`` is retryable and, when the retries are
exhausted, the row is marked failed and the pipeline moves on.
"""

from __future__ import annotations

import smtplib
import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.enums import NotificationChannel, NotificationStatus, RelevanceBand
from app.core.exceptions import NotificationError
from app.core.identity import normalize_text, utc_now
from app.core.logging import get_logger
from app.core.metrics import notifications_sent_total
from app.db.models.notification import Notification, UserPreference
from app.db.models.tender import Tender

logger = get_logger(__name__)

__all__ = ["MatchDecision", "NotificationService"]

#: Ordering used to compare a tender's band against a user's floor.
_BAND_RANK = {
    RelevanceBand.OUT_OF_SCOPE: -1,
    RelevanceBand.UNSCORED: 0,
    RelevanceBand.LOW_RELEVANCE: 1,
    RelevanceBand.RELEVANT: 2,
    RelevanceBand.HIGHLY_RELEVANT: 3,
}


@dataclass(slots=True)
class MatchDecision:
    """Why a user was, or was not, notified. Persisted for auditability."""

    matched: bool
    reasons: dict[str, Any] = field(default_factory=dict)
    rejected_by: str | None = None


class NotificationService:
    def __init__(self) -> None:
        settings = get_settings()
        self.email_enabled = settings.notifications.email_enabled
        self.smtp_host = settings.notifications.smtp_host
        self.smtp_port = settings.notifications.smtp_port
        self.smtp_user = settings.notifications.smtp_user
        self.smtp_password = settings.notifications.smtp_password
        self.smtp_tls = settings.notifications.smtp_tls
        self.smtp_timeout = settings.notifications.smtp_timeout_seconds
        self.from_address = settings.notifications.from_address
        self.email_min_band = settings.notifications.email_min_band

    # ------------------------------------------------------------------
    # Targeting
    # ------------------------------------------------------------------
    def evaluate(self, tender: Tender, preference: UserPreference) -> MatchDecision:
        """Decide whether this user should hear about this tender."""
        if not preference.active:
            return MatchDecision(False, rejected_by="inactive_user")

        band = RelevanceBand(tender.relevance_band or RelevanceBand.UNSCORED.value)
        if band is RelevanceBand.OUT_OF_SCOPE:
            return MatchDecision(False, rejected_by="out_of_scope")

        floor = RelevanceBand(preference.min_relevance_band or RelevanceBand.RELEVANT.value)
        if _BAND_RANK.get(band, 0) < _BAND_RANK.get(floor, 2):
            return MatchDecision(
                False,
                rejected_by="below_relevance_floor",
                reasons={"band": band.value, "floor": floor.value},
            )

        score = tender.relevance_score or 0.0
        if preference.min_score is not None and score < preference.min_score:
            return MatchDecision(False, rejected_by="below_score_floor")

        if preference.min_budget is not None:
            amount = float(tender.estimated_budget) if tender.estimated_budget else None
            # An unpublished budget must not silently exclude the tender: most
            # public notices omit it.
            if amount is not None and amount < preference.min_budget:
                return MatchDecision(False, rejected_by="below_budget_floor")

        blob = normalize_text(
            " ".join(
                part
                for part in (tender.title, tender.description, tender.buyer, tender.sector)
                if part
            )
        )

        for excluded in preference.excluded_keywords or []:
            if normalize_text(excluded) in blob:
                return MatchDecision(
                    False, rejected_by="excluded_keyword", reasons={"keyword": excluded}
                )

        reasons: dict[str, Any] = {"band": band.value, "score": tender.relevance_score}

        # Each dimension is a filter only when the user actually declared it:
        # an empty list means "no restriction", not "match nothing".
        if preference.connectors and tender.source_key not in preference.connectors:
            return MatchDecision(False, rejected_by="source_not_followed")

        if preference.countries:
            if not tender.country or not any(
                normalize_text(c) in normalize_text(tender.country)
                for c in preference.countries
            ):
                return MatchDecision(False, rejected_by="country_mismatch")
            reasons["country"] = tender.country

        if preference.sectors or preference.industries:
            wanted = [*(preference.sectors or []), *(preference.industries or [])]
            haystack = normalize_text(f"{tender.sector or ''} {tender.category or ''} {blob}")
            matched = [s for s in wanted if normalize_text(s) in haystack]
            if not matched:
                return MatchDecision(False, rejected_by="sector_mismatch")
            reasons["sectors"] = matched

        if preference.buyers:
            if not tender.buyer or not any(
                normalize_text(b) in normalize_text(tender.buyer) for b in preference.buyers
            ):
                return MatchDecision(False, rejected_by="buyer_mismatch")
            reasons["buyer"] = tender.buyer

        if preference.cpv_codes:
            codes = tender.cpv_codes or []
            matched_codes = [
                code
                for code in codes
                for prefix in preference.cpv_codes
                if code.startswith(str(prefix).rstrip("0") or str(prefix))
            ]
            if not matched_codes:
                return MatchDecision(False, rejected_by="cpv_mismatch")
            reasons["cpv_codes"] = matched_codes

        if preference.keywords:
            matched_keywords = [k for k in preference.keywords if normalize_text(k) in blob]
            if not matched_keywords:
                return MatchDecision(False, rejected_by="keyword_mismatch")
            reasons["keywords"] = matched_keywords

        return MatchDecision(True, reasons=reasons)

    # ------------------------------------------------------------------
    def under_daily_cap(self, session: Session, preference: UserPreference) -> bool:
        """Hard stop so a scraping burst cannot mail-bomb anyone."""
        cap = preference.max_notifications_per_day or 0
        if cap <= 0:
            return True
        since = utc_now() - timedelta(days=1)
        sent = session.execute(
            select(func.count(Notification.id)).where(
                Notification.user_id == preference.user_id,
                Notification.created_at >= since,
                Notification.status.in_(
                    [NotificationStatus.SENT.value, NotificationStatus.PENDING.value]
                ),
            )
        ).scalar_one()
        return int(sent) < cap

    # ------------------------------------------------------------------
    def build_for_tender(
        self, session: Session, tender: Tender
    ) -> list[Notification]:
        """Create pending notifications for every user this tender matches."""
        preferences = list(
            session.execute(select(UserPreference).where(UserPreference.active.is_(True)))
            .scalars()
            .all()
        )

        created: list[Notification] = []
        for preference in preferences:
            decision = self.evaluate(tender, preference)
            if not decision.matched:
                logger.debug(
                    "notification.skipped",
                    user_id=preference.user_id,
                    reason=decision.rejected_by,
                )
                continue

            if not self.under_daily_cap(session, preference):
                session.add(
                    self._notification(
                        preference,
                        tender,
                        NotificationChannel.IN_APP,
                        decision,
                        status=NotificationStatus.SUPPRESSED,
                        error="Daily notification cap reached.",
                    )
                )
                continue

            for channel_value in preference.channels or [NotificationChannel.IN_APP.value]:
                try:
                    channel = NotificationChannel(channel_value)
                except ValueError:
                    continue

                if channel is NotificationChannel.EMAIL:
                    if not self.email_enabled or not preference.email:
                        continue
                    band = RelevanceBand(tender.relevance_band or RelevanceBand.UNSCORED.value)
                    min_band = RelevanceBand(self.email_min_band)
                    if _BAND_RANK.get(band, 0) < _BAND_RANK.get(min_band, 2):
                        continue
                    # Digest subscribers get a batched email later; only the
                    # in-app row is created now.
                    if preference.digest_frequency != "immediate":
                        continue

                notification = self._notification(preference, tender, channel, decision)
                session.add(notification)
                created.append(notification)

        if created:
            logger.info(
                "notification.created",
                tender_uuid=str(tender.id),
                count=len(created),
            )
        return created

    def _notification(
        self,
        preference: UserPreference,
        tender: Tender,
        channel: NotificationChannel,
        decision: MatchDecision,
        *,
        status: NotificationStatus = NotificationStatus.PENDING,
        error: str | None = None,
    ) -> Notification:
        deadline = tender.deadline.strftime("%d/%m/%Y") if tender.deadline else "non publiée"
        band_label = (tender.relevance_band or "").replace("_", " ").title()
        subject = f"[{band_label}] {tender.title}"[:512]
        body = (
            f"{tender.title}\n\n"
            f"Acheteur    : {tender.buyer or 'non précisé'}\n"
            f"Pays        : {tender.country or 'non précisé'}\n"
            f"Date limite : {deadline}\n"
            f"Score       : {tender.relevance_score:.2f}\n"
            if tender.relevance_score is not None
            else ""
        ) + (
            f"Source      : {tender.source_key}\n"
            f"Lien        : {tender.source_url or '—'}\n"
        )
        return Notification(
            user_id=preference.user_id,
            tender_id=tender.id,
            channel=channel.value,
            status=status.value,
            subject=subject,
            body=body,
            payload={
                "tender_id": str(tender.id),
                "title": tender.title,
                "buyer": tender.buyer,
                "deadline": tender.deadline.isoformat() if tender.deadline else None,
                "score": tender.relevance_score,
                "band": tender.relevance_band,
                "source": tender.source_key,
                "url": tender.source_url,
            },
            match_reason=decision.reasons,
            error_message=error,
        )

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    def deliver(self, session: Session, notification: Notification) -> bool:
        """Send one notification. Returns success; records the outcome either way."""
        notification.attempts = (notification.attempts or 0) + 1

        if notification.channel == NotificationChannel.IN_APP.value:
            # In-app delivery *is* the persisted row: the API reads it.
            notification.status = NotificationStatus.SENT.value
            notification.sent_at = utc_now()
            notifications_sent_total.labels(channel="in_app", outcome="success").inc()
            return True

        if notification.channel != NotificationChannel.EMAIL.value:
            notification.status = NotificationStatus.FAILED.value
            notification.error_message = f"Unknown channel '{notification.channel}'."
            return False

        if not self.email_enabled:
            notification.status = NotificationStatus.SUPPRESSED.value
            notification.error_message = "Email delivery is disabled."
            return False

        preference = session.execute(
            select(UserPreference).where(UserPreference.user_id == notification.user_id)
        ).scalar_one_or_none()
        recipient = preference.email if preference else None
        if not recipient:
            notification.status = NotificationStatus.SUPPRESSED.value
            notification.error_message = "User has no email address configured."
            return False

        try:
            self._send_email(recipient, notification.subject or "", notification.body or "")
        except Exception as exc:
            notification.status = NotificationStatus.FAILED.value
            notification.error_message = str(exc)[:500]
            notifications_sent_total.labels(channel="email", outcome="failure").inc()
            logger.warning(
                "notification.delivery_failed",
                channel="email",
                user_id=notification.user_id,
                error=str(exc),
            )
            raise NotificationError(
                "Email delivery failed.",
                context={"user_id": notification.user_id},
                cause=exc,
            ) from exc

        notification.status = NotificationStatus.SENT.value
        notification.sent_at = utc_now()
        notifications_sent_total.labels(channel="email", outcome="success").inc()
        return True

    def _send_email(self, recipient: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)

        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.smtp_timeout) as smtp:
            if self.smtp_tls:
                smtp.starttls()
            if self.smtp_user:
                smtp.login(self.smtp_user, self.smtp_password)
            smtp.send_message(message)

    # ------------------------------------------------------------------
    # Digests
    # ------------------------------------------------------------------
    def build_digest(
        self, session: Session, preference: UserPreference, *, since
    ) -> Notification | None:
        """Batch a period's in-app notifications into one email.

        Digest subscribers still get every in-app notification as it happens —
        only the *email* is batched. Without this they received no email at all,
        which looked identical to the platform being broken.

        Returns ``None`` when the window produced nothing; an empty digest is
        worse than silence.
        """
        if preference.digest_frequency not in {"daily", "weekly"}:
            return None
        if not (self.email_enabled and preference.email):
            return None

        pending = list(
            session.execute(
                select(Notification)
                .where(
                    Notification.user_id == preference.user_id,
                    Notification.channel == NotificationChannel.IN_APP.value,
                    Notification.created_at >= since,
                    Notification.tender_id.isnot(None),
                )
                .order_by(Notification.created_at.desc())
                .limit(get_settings().notifications.max_items_per_digest)
            )
            .scalars()
            .all()
        )
        if not pending:
            logger.debug("digest.empty", user_id=preference.user_id)
            return None

        # Suppress a duplicate digest if one already went out for this window —
        # the sweep is periodic and at-least-once.
        already = session.execute(
            select(Notification.id).where(
                Notification.user_id == preference.user_id,
                Notification.channel == NotificationChannel.EMAIL.value,
                Notification.tender_id.is_(None),
                Notification.created_at >= since,
            )
        ).first()
        if already is not None:
            logger.debug("digest.already_sent", user_id=preference.user_id)
            return None

        digest = Notification(
            user_id=preference.user_id,
            # No tender_id: a digest is about many tenders, and this is also
            # what distinguishes it from a per-tender email above.
            tender_id=None,
            channel=NotificationChannel.EMAIL.value,
            status=NotificationStatus.PENDING.value,
            subject=self._digest_subject(preference, len(pending)),
            body=self._digest_body(pending),
            payload={
                "kind": "digest",
                "frequency": preference.digest_frequency,
                "count": len(pending),
                "tender_ids": [str(n.tender_id) for n in pending],
            },
            match_reason={"digest": preference.digest_frequency},
        )
        session.add(digest)
        logger.info(
            "digest.created",
            user_id=preference.user_id,
            count=len(pending),
            frequency=preference.digest_frequency,
        )
        return digest

    @staticmethod
    def _digest_subject(preference: UserPreference, count: int) -> str:
        period = "quotidien" if preference.digest_frequency == "daily" else "hebdomadaire"
        plural = "s" if count > 1 else ""
        return f"[SmartTender] Récapitulatif {period} — {count} appel{plural} d'offres"[:512]

    @staticmethod
    def _digest_body(notifications: list[Notification]) -> str:
        # Most relevant first: a digest people skim must lead with what matters.
        ordered = sorted(
            notifications,
            key=lambda n: (n.payload or {}).get("score") or 0.0,
            reverse=True,
        )
        lines = [
            f"{len(ordered)} appel(s) d'offres correspondant à vos critères :",
            "",
        ]
        for index, notification in enumerate(ordered, start=1):
            payload = notification.payload or {}
            deadline = payload.get("deadline")
            deadline_text = deadline[:10] if isinstance(deadline, str) else "non publiée"
            score = payload.get("score")
            score_text = f"{score:.2f}" if isinstance(score, (int, float)) else "—"
            lines.extend(
                [
                    f"{index}. {payload.get('title', '(sans titre)')}",
                    f"   Acheteur    : {payload.get('buyer') or 'non précisé'}",
                    f"   Date limite : {deadline_text}",
                    f"   Pertinence  : {score_text} ({payload.get('band', '—')})",
                    f"   Source      : {payload.get('source') or '—'}",
                    f"   Lien        : {payload.get('url') or '—'}",
                    "",
                ]
            )
        return "\n".join(lines)

    def build_all_digests(self, session: Session, *, frequency: str) -> list[Notification]:
        """Create one digest per subscriber on the given cadence."""
        from datetime import timedelta

        window = timedelta(days=1 if frequency == "daily" else 7)
        since = utc_now() - window

        preferences = list(
            session.execute(
                select(UserPreference).where(
                    UserPreference.active.is_(True),
                    UserPreference.digest_frequency == frequency,
                )
            )
            .scalars()
            .all()
        )

        digests = []
        for preference in preferences:
            try:
                digest = self.build_digest(session, preference, since=since)
            except Exception as exc:
                # One malformed profile must not cost every other subscriber
                # their digest.
                logger.warning(
                    "digest.build_failed",
                    user_id=preference.user_id,
                    error=str(exc)[:200],
                )
                continue
            if digest is not None:
                digests.append(digest)
        return digests

    # ------------------------------------------------------------------
    def mark_read(
        self, session: Session, notification_id: uuid_module.UUID, user_id: str
    ) -> bool:
        notification = session.get(Notification, notification_id)
        if notification is None or notification.user_id != user_id:
            return False
        notification.status = NotificationStatus.READ.value
        notification.read_at = utc_now()
        return True
