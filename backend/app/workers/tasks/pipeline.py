"""Post-ingestion pipeline: attachments, scoring, notification hand-off.

Every task here takes a tender UUID and nothing else. That is what makes them
idempotent and replay-safe: a task re-delivered after a worker crash reads the
current state from the database, recomputes, and writes the same result. No
task depends on another's return value, so any of them can be retried alone.

``process_tender`` is the entry point. It advances the pipeline state, kicks
off attachment downloads, then chains scoring — which in turn hands off to
notifications. Each hop is a separate queue, so a slow stage backs up its own
queue rather than the whole pipeline.
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Any

from app.connectors.models import NormalizedTender
from app.core.enums import (
    PipelineStage,
    ProcurementType,
    RelevanceBand,
    TenderPipelineState,
    TenderStatus,
)
from app.core.exceptions import SmartTenderError
from app.core.identity import utc_now
from app.core.logging import get_logger, log_context
from app.core.metrics import observe_stage, tenders_processed_total
from app.db.models.tender import Tender, TenderDocument, TenderScore
from app.db.session import session_scope
from app.services import audit
from app.services.scoring import get_scoring_engine
from app.workers.celery_app import celery_app
from app.workers.tasks.base import PipelineTask

logger = get_logger(__name__)

__all__ = [
    "download_documents",
    "extract_tender_text",
    "process_tender",
    "rescore_all",
    "score_tender",
]


def _to_normalized(row: Tender) -> NormalizedTender:
    """Project a persisted tender back onto the connector's canonical model.

    Scoring is defined over ``NormalizedTender`` so that the same code scores a
    freshly scraped tender and a re-scored historical one identically.
    """
    return NormalizedTender(
        connector_key=row.source_key,
        source_url=row.source_url,
        canonical_url=row.canonical_url,
        external_id=row.external_id,
        reference=row.reference,
        title=row.title,
        description=row.description,
        buyer=row.buyer,
        funding_organization=row.funding_organization,
        contact_email=row.contact_email,
        language=row.language,
        country=row.country,
        location=row.location,
        sector=row.sector,
        category=row.category,
        cpv_codes=list(row.cpv_codes or []),
        procurement_type=ProcurementType(row.procurement_type or ProcurementType.UNKNOWN.value),
        status=TenderStatus(row.status or TenderStatus.UNKNOWN.value),
        publication_date=row.publication_date,
        deadline=row.deadline,
        estimated_budget=row.estimated_budget,
        currency=row.currency,
        raw_sha256=row.raw_sha256,
        text_sha256=row.text_sha256,
        full_text=row.extracted_text,
        extra=dict(row.extra or {}),
    )


# ---------------------------------------------------------------------------
@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.pipeline.process_tender",
    queue="parsing",
)
def process_tender(self: PipelineTask, tender_id: str) -> dict[str, Any]:
    """Advance a freshly ingested tender through the pipeline."""
    with log_context(tender_uuid=tender_id):
        with session_scope() as session:
            tender = session.get(Tender, uuid_module.UUID(tender_id))
            if tender is None:
                # The row is genuinely gone (deleted, or a stale replay). This
                # is not an error worth retrying.
                logger.warning("pipeline.tender_missing", tender_uuid=tender_id)
                return {"tender_id": tender_id, "status": "missing"}

            if tender.pipeline_state == TenderPipelineState.COMPLETED.value:
                # Idempotent replay: nothing to redo.
                return {"tender_id": tender_id, "status": "already_completed"}

            tender.pipeline_state = TenderPipelineState.PARSING.value
            has_documents = bool(tender.documents)

        # Ordering is load-bearing: scoring reads the extracted document text,
        # and extraction reads the downloaded attachments. Each stage dispatches
        # the next rather than running in parallel, so nothing is scored on a
        # title while its cahier des charges is still downloading.
        #
        #   download_documents → extract_tender_text → score_tender → notify
        #
        # These are separate queues, so a slow OCR job backs up OCR alone.
        if has_documents:
            download_documents.apply_async(
                kwargs={"tender_id": tender_id}, queue="parsing"
            )
        else:
            extract_tender_text.apply_async(
                kwargs={"tender_id": tender_id}, queue="ocr"
            )
        return {"tender_id": tender_id, "status": "dispatched"}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.pipeline.extract_tender_text",
    queue="ocr",
    max_retries=1,
)
def extract_tender_text(self: PipelineTask, tender_id: str) -> dict[str, Any]:
    """Read the tender's documents into searchable text, then hand off to scoring.

    Best-effort throughout: a document that cannot be read costs the tender
    some scoring signal, never the tender itself. The task always dispatches
    scoring, so an extraction failure can never strand a tender unscored.
    """
    from app.core.config import get_settings
    from app.services.extraction import get_extractor
    from app.services.storage import get_storage

    with log_context(tender_uuid=tender_id):
        settings = get_settings().extraction
        outcome: dict[str, Any] = {"tender_id": tender_id}

        try:
            with session_scope() as session:
                tender = session.get(Tender, uuid_module.UUID(tender_id))
                if tender is None:
                    logger.warning("extraction.tender_missing", tender_uuid=tender_id)
                    return {"tender_id": tender_id, "status": "missing"}

                if not settings.enabled:
                    tender.extraction_status = "skipped"
                    outcome["status"] = "skipped"
                else:
                    sources: list[tuple[str, str | None, str | None]] = []
                    if tender.storage_key:
                        sources.append(
                            (tender.storage_key, tender.content_type, tender.original_filename)
                        )
                    sources.extend(
                        (doc.storage_key, doc.content_type, doc.name)
                        for doc in tender.documents
                        if doc.status == "stored" and doc.storage_key
                    )

                    if not sources:
                        tender.extraction_status = "empty"
                        tender.extraction_method = "none"
                        outcome["status"] = "nothing_to_extract"
                    else:
                        outcome.update(
                            _run_extraction(
                                session, tender, sources, get_storage(), get_extractor()
                            )
                        )
        except Exception as exc:
            # Never strand the tender: record the failure and let it be scored
            # on what it already has.
            logger.warning(
                "extraction.failed",
                error=str(exc)[:300],
                error_type=type(exc).__name__,
            )
            outcome["status"] = "failed"
            outcome["error"] = type(exc).__name__

        score_tender.apply_async(kwargs={"tender_id": tender_id}, queue="scoring")
        return outcome


def _run_extraction(session, tender, sources, storage, extractor) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    """Extract every source document and merge the results onto the tender."""
    from app.core.config import get_settings
    from app.services.extraction import clean_extracted_text

    limit = get_settings().extraction.max_chars_per_tender
    collected: list[str] = []
    methods: set[str] = set()
    pages_ocr = 0
    errors: list[str] = []

    for key, content_type, name in sources:
        try:
            content = storage.get_bytes(key)
        except Exception as exc:
            errors.append(f"{name or key}: unreadable from storage ({type(exc).__name__})")
            continue

        result = extractor.extract(content, content_type=content_type, filename=name)
        if result.ok:
            collected.append(result.text)
            methods.add(result.method)
            pages_ocr += result.pages_ocr
        elif result.error:
            errors.append(f"{name or key}: {result.error}")

        for warning in result.warnings:
            logger.info("extraction.warning", document=name or key, warning=warning)

    text, truncated = clean_extracted_text("\n\n".join(collected), max_chars=limit)

    tender.extracted_text = text or None
    tender.extraction_chars = len(text)
    tender.extraction_error = "; ".join(errors)[:2000] or None

    if text:
        tender.extraction_status = "extracted"
        tender.extraction_method = "mixed" if len(methods) > 1 else next(iter(methods), "digital")
    else:
        tender.extraction_status = "failed" if errors else "empty"
        tender.extraction_method = "none"

    # A tender whose description was never published gets the extracted text as
    # its description, which is what makes it readable in the dashboard.
    if text and not (tender.description or "").strip():
        tender.description = text[:4000]

    audit.record_event(
        session,
        "extraction.completed",
        stage=PipelineStage.EXTRACT,
        tender_id=tender.id,
        connector=tender.source_key,
        message=f"Extracted {len(text)} characters from {len(sources)} document(s).",
        context={
            "documents": len(sources),
            "method": tender.extraction_method,
            "pages_ocr": pages_ocr,
            "truncated": truncated,
            "errors": errors[:5],
        },
    )
    logger.info(
        "extraction.completed",
        chars=len(text),
        documents=len(sources),
        method=tender.extraction_method,
        pages_ocr=pages_ocr,
    )
    return {
        "status": tender.extraction_status,
        "chars": len(text),
        "method": tender.extraction_method,
        "pages_ocr": pages_ocr,
        "documents": len(sources),
    }


# ---------------------------------------------------------------------------
@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.pipeline.score_tender",
    queue="scoring",
)
def score_tender(self: PipelineTask, tender_id: str) -> dict[str, Any]:
    """Compute and persist the relevance score, then hand off to notifications."""
    with log_context(tender_uuid=tender_id):
        engine = get_scoring_engine()

        try:
            with session_scope() as session:
                tender = session.get(Tender, uuid_module.UUID(tender_id))
                if tender is None:
                    logger.warning("scoring.tender_missing", tender_uuid=tender_id)
                    return {"tender_id": tender_id, "status": "missing"}

                tender.pipeline_state = TenderPipelineState.SCORING.value

                with observe_stage(PipelineStage.SCORE.value):
                    result = engine.score(
                        _to_normalized(tender),
                        context={"history_provider": _history_provider(session)},
                    )

                tender.relevance_score = result.score
                tender.relevance_band = result.band.value
                tender.score_profile_version = result.profile_version
                tender.scored_at = utc_now()
                tender.pipeline_state = TenderPipelineState.SCORED.value

                session.add(
                    TenderScore(
                        tender_id=tender.id,
                        profile_name=result.profile_name,
                        profile_version=result.profile_version,
                        score=result.score,
                        band=result.band.value,
                        breakdown=result.breakdown,
                        weights=result.weights,
                        duration_ms=result.duration_ms,
                    )
                )

                audit.record_event(
                    session,
                    "scoring.completed",
                    stage=PipelineStage.SCORE,
                    tender_id=tender.id,
                    connector=tender.source_key,
                    duration_ms=result.duration_ms,
                    message=f"Scored {result.score:.3f} ({result.band.value}).",
                    context={
                        "profile_version": result.profile_version,
                        "veto_reason": result.veto_reason,
                    },
                )
                band = result.band
                score = result.score

        except SmartTenderError as exc:
            self.retry_or_fail(exc)
            raise

        # Out-of-scope tenders are stored and searchable but never announced.
        if band is not RelevanceBand.OUT_OF_SCOPE:
            from app.workers.tasks.notifications import dispatch_tender_notifications

            dispatch_tender_notifications.apply_async(
                kwargs={"tender_id": tender_id}, queue="notifications"
            )
        else:
            _complete(tender_id)

        logger.info("scoring.completed", score=round(score, 4), band=band.value)
        return {"tender_id": tender_id, "score": score, "band": band.value}


def _history_provider(session: Any):
    """Supply past win/loss counts to the historical-success criterion.

    Counts only *decided* submissions: a bid still awaiting an award decision —
    which is most of them, for months — says nothing about our win rate, and
    counting it as a loss would penalise every buyer with a slow procurement
    cycle.

    Prefers the buyer's own record and falls back to the sector, because a buyer
    we have never bid with is the common case and a sector average is still
    better evidence than nothing. Returns ``None`` when there is no history at
    all, which makes the criterion abstain rather than invent a number.
    """
    from sqlalchemy import case, func, select

    from app.core.enums import SubmissionOutcome
    from app.db.models.submission import Submission

    decided = (
        SubmissionOutcome.WON.value,
        SubmissionOutcome.LOST.value,
    )
    wins = func.sum(
        case((Submission.outcome == SubmissionOutcome.WON.value, 1), else_=0)
    )

    def _count(column: Any, value: str | None) -> tuple[int, int] | None:
        if not value:
            return None
        row = session.execute(
            select(wins, func.count(Submission.id)).where(
                column == value, Submission.outcome.in_(decided)
            )
        ).first()
        if row is None or not row[1]:
            return None
        return int(row[0] or 0), int(row[1])

    def provider(tender: NormalizedTender) -> tuple[int, int] | None:
        return _count(Submission.buyer, tender.buyer) or _count(
            Submission.sector, tender.sector
        )

    return provider


# ---------------------------------------------------------------------------
@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.pipeline.download_documents",
    queue="parsing",
    max_retries=2,
)
def download_documents(self: PipelineTask, tender_id: str) -> dict[str, Any]:
    """Fetch the notice's attachments into object storage.

    Best-effort by design: attachments are large, portals serve them
    inconsistently, and a missing annex must never hold up a tender that is
    otherwise ready to be shown and acted on.
    """
    import asyncio

    with log_context(tender_uuid=tender_id):
        with session_scope() as session:
            tender = session.get(Tender, uuid_module.UUID(tender_id))
            if tender is None:
                return {"tender_id": tender_id, "status": "missing"}
            connector_key = tender.source_key
            pending = [
                (str(doc.id), doc.source_url, doc.name)
                for doc in tender.documents
                if doc.status == "pending" and doc.source_url
            ]

        if not pending:
            _continue_to_extraction(tender_id)
            return {"tender_id": tender_id, "downloaded": 0}

        try:
            results = asyncio.run(_download_all(connector_key, tender_id, pending))
        except Exception as exc:
            # Attachments are best-effort. The tender must still be extracted
            # and scored on whatever it already has.
            logger.warning(
                "documents.download_batch_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            _continue_to_extraction(tender_id)
            return {"tender_id": tender_id, "downloaded": 0, "error": type(exc).__name__}

        stored = 0
        with session_scope() as session:
            for document_id, outcome in results.items():
                document = session.get(TenderDocument, uuid_module.UUID(document_id))
                if document is None:
                    continue
                if outcome.get("ok"):
                    document.status = "stored"
                    document.storage_bucket = outcome["bucket"]
                    document.storage_key = outcome["key"]
                    document.content_type = outcome["content_type"]
                    document.size_bytes = outcome["size_bytes"]
                    document.sha256 = outcome["sha256"]
                    document.downloaded_at = utc_now()
                    stored += 1
                else:
                    document.status = "failed"
                    document.error_message = str(outcome.get("error"))[:500]

        logger.info("documents.downloaded", stored=stored, total=len(pending))
        _continue_to_extraction(tender_id)
        return {"tender_id": tender_id, "downloaded": stored, "total": len(pending)}


def _continue_to_extraction(tender_id: str) -> None:
    """Hand a tender on to extraction.

    Called from every exit of ``download_documents``, including the failure
    paths — a tender whose attachments could not be fetched must still be
    extracted and scored, not silently abandoned mid-pipeline.
    """
    extract_tender_text.apply_async(kwargs={"tender_id": tender_id}, queue="ocr")


async def _download_all(
    connector_key: str, tender_id: str, pending: list[tuple[str, str, str | None]]
) -> dict[str, dict[str, Any]]:
    from app.connectors.http.client import ResilientHttpClient
    from app.connectors.registry import get_registry
    from app.core.identity import sha256_bytes
    from app.services.storage import get_storage

    registry = get_registry()
    try:
        config = registry.config(connector_key)
        http_config = config.http
        policy = config.documents_policy
        base_url = config.base_url
    except Exception:
        from app.core.config import load_yaml_config

        http_config = load_yaml_config("http")
        policy = {}
        base_url = ""

    max_bytes = int(policy.get("max_bytes_each") or 25 * 1024 * 1024)
    max_documents = int(policy.get("max_per_tender") or 10)
    storage = get_storage()
    results: dict[str, dict[str, Any]] = {}

    async with ResilientHttpClient(
        connector_key=connector_key, config=http_config, base_url=base_url
    ) as client:
        for document_id, url, name in pending[:max_documents]:
            try:
                page = await client.get(url, max_bytes=max_bytes, check_robots=False)
                key = storage.build_key(tender_id, name or "attachment", subpath="documents")
                stored = storage.put_bytes(
                    key,
                    page.content,
                    content_type=page.headers.get("content-type", "application/octet-stream"),
                    metadata={"tender-id": tender_id},
                )
                results[document_id] = {
                    "ok": True,
                    "bucket": stored.bucket,
                    "key": stored.key,
                    "content_type": stored.content_type,
                    "size_bytes": stored.size_bytes,
                    "sha256": sha256_bytes(page.content),
                }
            except Exception as exc:
                results[document_id] = {"ok": False, "error": str(exc)}

    for document_id, _url, _name in pending[max_documents:]:
        results[document_id] = {"ok": False, "error": "Exceeded the per-tender document limit."}
    return results


# ---------------------------------------------------------------------------
def _complete(tender_id: str) -> None:
    with session_scope() as session:
        tender = session.get(Tender, uuid_module.UUID(tender_id))
        if tender is not None:
            tender.pipeline_state = TenderPipelineState.COMPLETED.value
    tenders_processed_total.labels(outcome="completed").inc()


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.pipeline.complete_tender",
    queue="parsing",
)
def complete_tender(self: PipelineTask, tender_id: str) -> dict[str, Any]:
    _complete(tender_id)
    return {"tender_id": tender_id, "status": "completed"}


@celery_app.task(
    base=PipelineTask,
    bind=True,
    name="app.workers.tasks.pipeline.rescore_all",
    queue="scoring",
)
def rescore_all(self: PipelineTask, limit: int = 5000, band: str | None = None) -> dict[str, Any]:
    """Re-score existing tenders after a weights change.

    Dispatched as individual tasks rather than one long loop, so the sweep is
    interruptible, parallel across workers, and cannot monopolise a slot.
    """
    from sqlalchemy import select

    from app.services.scoring import reset_scoring_engine

    reset_scoring_engine()

    with session_scope() as session:
        query = select(Tender.id).order_by(Tender.created_at.desc()).limit(limit)
        if band:
            query = query.where(Tender.relevance_band == band)
        tender_ids = [str(row) for row in session.execute(query).scalars().all()]

    for tender_id in tender_ids:
        score_tender.apply_async(kwargs={"tender_id": tender_id}, queue="scoring")

    logger.info("scoring.rescore_dispatched", count=len(tender_ids))
    return {"dispatched": len(tender_ids)}
