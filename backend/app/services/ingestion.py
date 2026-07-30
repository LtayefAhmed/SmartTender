"""Ingestion — the narrow gate every tender passes through, from any entry point.

Manual upload, manual scrape and scheduled scrape all converge here, which is
why "the pipeline behaves the same regardless of how a tender arrived" is true
by construction rather than by discipline.

The sequence is fixed:

    deduplicate  ->  mint UUID  ->  store bytes  ->  persist metadata  ->  enqueue

and the ordering is load-bearing. Deduplicating *before* minting a UUID means a
duplicate never consumes an identifier, an object or a queue slot. Storing
bytes *before* the row means a committed row always points at an object that
exists. Enqueueing *after* the commit means a worker can never pick up a task
for a row that is not yet visible — the classic race that produces "tender not
found" errors under load.

Nothing heavy happens here. This function is called from inside an HTTP request
and must return in milliseconds; parsing, OCR, scoring and notification are all
someone else's problem, reached through the queue.
"""

from __future__ import annotations

import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.connectors.models import NormalizedTender
from app.core.enums import EntryPoint, PipelineStage, TenderPipelineState
from app.core.exceptions import StorageError
from app.core.identity import content_fingerprint, new_tender_uuid, utc_now
from app.core.logging import get_logger, log_context
from app.core.metrics import (
    document_size_bytes,
    observe_stage,
    tenders_ingested_total,
    tenders_rejected_total,
)
from app.db.models.source import Source
from app.db.models.tender import Tender, TenderDocument
from app.services import audit
from app.services.deduplication import DeduplicationService, DuplicateVerdict
from app.services.storage import get_storage

logger = get_logger(__name__)

__all__ = ["IngestionResult", "IngestionService"]


@dataclass(slots=True)
class IngestionResult:
    """What happened to one incoming record."""

    accepted: bool
    tender_id: uuid_module.UUID | None = None
    duplicate_of: uuid_module.UUID | None = None
    duplicate_strategy: str | None = None
    reason: str | None = None
    storage_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "tender_id": str(self.tender_id) if self.tender_id else None,
            "duplicate_of": str(self.duplicate_of) if self.duplicate_of else None,
            "duplicate_strategy": self.duplicate_strategy,
            "reason": self.reason,
        }


class IngestionService:
    """Admits records into the pipeline."""

    def __init__(self, dedup: DeduplicationService | None = None) -> None:
        self.dedup = dedup or DeduplicationService()

    # ------------------------------------------------------------------
    def ingest_tender(
        self,
        session: Session,
        tender: NormalizedTender,
        *,
        entry_point: EntryPoint,
        source: Source | None = None,
        job_id: uuid_module.UUID | None = None,
        raw_content: bytes | None = None,
        content_type: str | None = None,
        original_filename: str | None = None,
        enqueue: Callable[[uuid_module.UUID], None] | None = None,
        actor: str | None = None,
    ) -> IngestionResult:
        """Admit one normalised tender. Never raises for a duplicate."""
        fingerprint = content_fingerprint(
            raw=raw_content,
            text=tender.comparison_text(),
        )
        raw_sha = tender.raw_sha256 or fingerprint.get("raw_sha256")
        text_sha = tender.text_sha256 or fingerprint.get("text_sha256")

        # --- deduplicate ---------------------------------------------
        with observe_stage(PipelineStage.DEDUPLICATE.value):
            verdict = self.dedup.check(
                session, tender, raw_sha256=raw_sha, text_sha256=text_sha
            )

        if verdict.is_duplicate:
            return self._reject_duplicate(
                session, tender, verdict, job_id=job_id, raw_sha=raw_sha, text_sha=text_sha
            )

        # --- mint the master identifier -------------------------------
        tender_id = new_tender_uuid()

        with log_context(tender_uuid=str(tender_id)):
            storage_key: str | None = None
            storage_bucket: str | None = None
            size_bytes: int | None = None

            # --- store bytes before the row -------------------------
            if raw_content:
                try:
                    with observe_stage(PipelineStage.STORE.value):
                        stored = get_storage().put_bytes(
                            get_storage().build_key(
                                str(tender_id), original_filename or "source.html"
                            ),
                            raw_content,
                            content_type=content_type or "application/octet-stream",
                            metadata={
                                "tender-id": str(tender_id),
                                "connector": tender.connector_key,
                                "entry-point": entry_point.value,
                            },
                        )
                    storage_key, storage_bucket = stored.key, stored.bucket
                    size_bytes = stored.size_bytes
                    document_size_bytes.labels(entry_point=entry_point.value).observe(
                        stored.size_bytes
                    )
                except StorageError as exc:
                    # The metadata is still worth keeping: a tender we can see
                    # but whose original we failed to archive is far better than
                    # no tender at all. The pipeline state records the gap.
                    logger.error(
                        "ingestion.storage_failed.continuing",
                        error=exc.message,
                        connector=tender.connector_key,
                        # Carry the cause and hint through: this branch keeps
                        # the tender and drops the original, so the log line is
                        # the only place the failure is ever explained.
                        **exc.context,
                    )
                    audit.record_error(
                        session,
                        "ingestion.storage_failed",
                        exc,
                        stage=PipelineStage.STORE,
                        tender_id=tender_id,
                        connector=tender.connector_key,
                    )

            # --- persist metadata -----------------------------------
            row = self._build_row(
                tender_id=tender_id,
                tender=tender,
                entry_point=entry_point,
                source=source,
                raw_sha=raw_sha,
                text_sha=text_sha,
                storage_bucket=storage_bucket,
                storage_key=storage_key,
                size_bytes=size_bytes,
                content_type=content_type,
                original_filename=original_filename,
            )
            session.add(row)

            for document in tender.documents:
                session.add(
                    TenderDocument(
                        tender_id=tender_id,
                        name=document.name,
                        source_url=document.url,
                        content_type=document.content_type,
                        size_bytes=document.size_bytes,
                        status="pending",
                    )
                )

            try:
                session.flush()
            except IntegrityError:
                # Two workers raced on the same canonical URL. The unique index
                # is the real arbiter of duplication; losing the race is a
                # duplicate, not an error.
                session.rollback()
                logger.info(
                    "ingestion.race_lost.treated_as_duplicate",
                    connector=tender.connector_key,
                    canonical_url=tender.canonical_url,
                )
                tenders_rejected_total.labels(
                    entry_point=entry_point.value, reason="duplicate_race"
                ).inc()
                return IngestionResult(
                    accepted=False,
                    reason="duplicate",
                    duplicate_strategy="unique_constraint",
                )

            if source is not None:
                source.total_items_ingested = (source.total_items_ingested or 0) + 1

            audit.record_event(
                session,
                "ingestion.accepted",
                stage=PipelineStage.INGEST,
                tender_id=tender_id,
                job_id=job_id,
                connector=tender.connector_key,
                url=tender.source_url,
                message=tender.title[:400],
                actor=actor,
                context={
                    "entry_point": entry_point.value,
                    "storage_key": storage_key,
                    "documents": len(tender.documents),
                },
            )

            tenders_ingested_total.labels(
                connector=tender.connector_key, entry_point=entry_point.value
            ).inc()

            # --- enqueue AFTER the caller commits --------------------
            if enqueue is not None:
                _enqueue_after_commit(session, enqueue, tender_id)

            logger.info(
                "ingestion.accepted",
                connector=tender.connector_key,
                entry_point=entry_point.value,
                title=tender.title[:120],
            )
            return IngestionResult(
                accepted=True, tender_id=tender_id, storage_key=storage_key
            )

    # ------------------------------------------------------------------
    def _reject_duplicate(
        self,
        session: Session,
        tender: NormalizedTender,
        verdict: DuplicateVerdict,
        *,
        job_id: uuid_module.UUID | None,
        raw_sha: str | None,
        text_sha: str | None,
    ) -> IngestionResult:
        self.dedup.record_duplicate(
            session,
            tender,
            verdict,
            job_id=job_id,
            raw_sha256=raw_sha,
            text_sha256=text_sha,
        )
        audit.record_event(
            session,
            "dedup.rejected",
            stage=PipelineStage.DEDUPLICATE,
            connector=tender.connector_key,
            tender_id=verdict.canonical_id,
            job_id=job_id,
            url=tender.source_url,
            message=tender.title[:400],
            context=verdict.as_context(),
        )
        tenders_rejected_total.labels(
            entry_point="scrape", reason="duplicate"
        ).inc()
        return IngestionResult(
            accepted=False,
            duplicate_of=verdict.canonical_id,
            duplicate_strategy=verdict.strategy.value if verdict.strategy else None,
            reason="duplicate",
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _build_row(
        *,
        tender_id: uuid_module.UUID,
        tender: NormalizedTender,
        entry_point: EntryPoint,
        source: Source | None,
        raw_sha: str | None,
        text_sha: str | None,
        storage_bucket: str | None,
        storage_key: str | None,
        size_bytes: int | None,
        content_type: str | None,
        original_filename: str | None,
    ) -> Tender:
        now = utc_now()
        return Tender(
            id=tender_id,
            source_id=source.id if source else None,
            source_key=tender.connector_key,
            entry_point=entry_point.value,
            source_url=tender.source_url,
            canonical_url=tender.canonical_url,
            external_id=tender.external_id,
            reference=tender.reference,
            title=tender.title,
            description=tender.description,
            buyer=tender.buyer,
            funding_organization=tender.funding_organization,
            contact_email=tender.contact_email,
            language=tender.language,
            country=tender.country,
            location=tender.location,
            sector=tender.sector,
            category=tender.category,
            cpv_codes=tender.cpv_codes,
            procurement_type=tender.procurement_type.value,
            status=tender.status.value,
            publication_date=tender.publication_date,
            deadline=tender.deadline,
            estimated_budget=tender.estimated_budget,
            currency=tender.currency,
            raw_sha256=raw_sha,
            text_sha256=text_sha,
            storage_bucket=storage_bucket,
            storage_key=storage_key,
            original_filename=original_filename,
            content_type=content_type,
            size_bytes=size_bytes,
            # RECEIVED, not QUEUED: the task is only published after commit.
            pipeline_state=TenderPipelineState.RECEIVED.value,
            first_seen_at=now,
            last_seen_at=now,
            ingested_at=now,
            seen_on_sources=[tender.connector_key],
            extra=tender.extra,
        )


def _enqueue_after_commit(
    session: Session, enqueue: Callable[[uuid_module.UUID], None], tender_id: uuid_module.UUID
) -> None:
    """Publish the task only once the transaction is durably committed.

    Publishing inside the transaction is a classic and genuinely nasty bug:
    Redis is faster than PostgreSQL's commit, so the worker starts, queries for
    the row, finds nothing, and fails — intermittently, and only under load.
    """
    from sqlalchemy import event

    def _publish(_session: Session) -> None:
        try:
            enqueue(tender_id)
        except Exception as exc:
            # The row is committed and the reconciliation job will pick it up;
            # a broker hiccup must not roll back an accepted tender.
            logger.error(
                "ingestion.enqueue_failed",
                tender_uuid=str(tender_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    # ``once=True`` rather than removing the listener from inside itself:
    # self-removal mutates the collection SQLAlchemy is iterating, which raises
    # as soon as two tenders are ingested in one transaction — i.e. on every
    # real scraping run.
    event.listen(session, "after_commit", _publish, once=True)
