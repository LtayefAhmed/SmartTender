"""Duplicate detection — three escalating stages, cheapest first.

    1. canonical URL   indexed equality      catches re-crawls
    2. content hash    indexed equality      catches the same file, two portals
    3. semantic        bounded comparison    catches re-publications

Ordering is not an implementation detail, it is the performance design. Stages
1 and 2 are single indexed lookups and resolve the overwhelming majority of
incoming records; stage 3 only ever runs on what survives, and even then only
against a pre-filtered candidate window. That is what keeps duplicate detection
O(1) per record at 500 tenders arriving at once, instead of O(n²) across the
whole corpus.

A confirmed duplicate is **rejected but never discarded**: the evidence is
written to ``duplicate_records`` and the new source is attached to the
canonical tender, so the dashboard can say "seen on 3 portals" and an operator
can always answer "why is this tender not showing up?".
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from app.connectors.models import NormalizedTender
from app.core.config import load_yaml_config
from app.core.enums import DuplicateStrategy
from app.core.identity import canonicalize_url, normalize_text, utc_now
from app.core.logging import get_logger
from app.core.metrics import duplicates_detected_total
from app.db.models.tender import DuplicateRecord, Tender
from app.services.similarity import get_similarity_backend

logger = get_logger(__name__)

__all__ = ["DeduplicationService", "DuplicateVerdict"]


@dataclass(slots=True)
class DuplicateVerdict:
    """The outcome of running a record through the three stages."""

    is_duplicate: bool
    strategy: DuplicateStrategy | None = None
    canonical_id: uuid_module.UUID | None = None
    similarity: float | None = None
    matched_on: dict[str, Any] = field(default_factory=dict)

    def as_context(self) -> dict[str, Any]:
        return {
            "duplicate": self.is_duplicate,
            "strategy": self.strategy.value if self.strategy else None,
            "canonical_id": str(self.canonical_id) if self.canonical_id else None,
            "similarity": self.similarity,
        }


class DeduplicationService:
    """Stateless service; one instance per task is fine and cheap."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = config or load_yaml_config("dedup")
        self.enabled = bool(self.config.get("enabled", True))

        url_cfg = self.config.get("canonical_url") or {}
        self.url_enabled = bool(url_cfg.get("enabled", True))
        self.strip_params = list(url_cfg.get("strip_query_params") or [])
        self.url_options = {
            "strip_fragment": bool(url_cfg.get("strip_fragment", True)),
            "lowercase_host": bool(url_cfg.get("lowercase_host", True)),
            "drop_default_port": bool(url_cfg.get("drop_default_port", True)),
            "drop_trailing_slash": bool(url_cfg.get("drop_trailing_slash", True)),
            "sort_query": bool(url_cfg.get("sort_query_params", True)),
        }

        hash_cfg = self.config.get("content_hash") or {}
        self.hash_enabled = bool(hash_cfg.get("enabled", True))
        self.hash_raw = bool(hash_cfg.get("hash_raw_bytes", True))
        self.hash_text = bool(hash_cfg.get("hash_normalised_text", True))

        semantic_cfg = self.config.get("semantic") or {}
        self.semantic_enabled = bool(semantic_cfg.get("enabled", True))
        self.threshold = float(semantic_cfg.get("threshold") or 0.92)
        self.field_weights: dict[str, float] = dict(semantic_cfg.get("fields") or {"title": 1.0})
        candidate_cfg = semantic_cfg.get("candidate_filter") or {}
        self.lookback_days = int(candidate_cfg.get("lookback_days") or 120)
        self.deadline_tolerance_days = int(candidate_cfg.get("deadline_tolerance_days") or 3)
        self.max_candidates = int(candidate_cfg.get("max_candidates") or 200)
        self.same_country = bool(candidate_cfg.get("same_country", True))

        action_cfg = self.config.get("on_duplicate") or {}
        self.record_evidence = bool(action_cfg.get("record_evidence", True))
        self.attach_source = bool(action_cfg.get("attach_source_to_canonical", True))

    # ------------------------------------------------------------------
    def canonicalize(self, url: str | None) -> str | None:
        if not url or not self.url_enabled:
            return url
        return canonicalize_url(url, strip_params=self.strip_params, **self.url_options)

    def _weighted_key(self, values: dict[str, str | None]) -> str:
        """Weighted concatenation of the fields that identify a tender.

        Repeating a field proportionally to its weight is a crude but effective
        way to weight a bag-of-tokens representation without the backend
        needing to know anything about tender structure.

        Both sides of every comparison must be built by this one function.
        Building the incoming record differently from the stored candidate
        means two *identical* tenders do not score 1.0, which silently pushes
        every real duplicate below the threshold.
        """
        parts: list[str] = []
        for field_name, weight in self.field_weights.items():
            value = values.get(field_name)
            if not value:
                continue
            repeats = max(1, round(weight * 2))
            parts.extend([normalize_text(value)] * repeats)
        return " ".join(part for part in parts if part)

    def comparison_key(self, tender: NormalizedTender) -> str:
        return self._weighted_key(
            {
                "title": tender.title,
                "buyer": tender.buyer,
                "reference": tender.reference,
                "description": (tender.description or "")[:2000],
            }
        )

    def candidate_key(self, candidate: Tender) -> str:
        return self._weighted_key(
            {
                "title": candidate.title,
                "buyer": candidate.buyer,
                "reference": candidate.reference,
                "description": (candidate.description or "")[:2000],
            }
        )

    # ------------------------------------------------------------------
    def check(
        self,
        session: Session,
        tender: NormalizedTender,
        *,
        raw_sha256: str | None = None,
        text_sha256: str | None = None,
    ) -> DuplicateVerdict:
        """Run the three stages. Returns a verdict; never raises for a duplicate."""
        if not self.enabled:
            return DuplicateVerdict(is_duplicate=False)

        # --- stage 1: canonical URL -----------------------------------
        canonical = self.canonicalize(tender.canonical_url or tender.source_url)
        if self.url_enabled and canonical:
            existing = session.execute(
                select(Tender.id).where(Tender.canonical_url == canonical).limit(1)
            ).scalar_one_or_none()
            if existing:
                return self._hit(
                    tender, DuplicateStrategy.CANONICAL_URL, existing, matched={"url": canonical}
                )

        # --- stage 1b: portal-native identifier ------------------------
        if tender.external_id:
            existing = session.execute(
                select(Tender.id)
                .where(
                    Tender.source_key == tender.connector_key,
                    Tender.external_id == tender.external_id,
                )
                .limit(1)
            ).scalar_one_or_none()
            if existing:
                return self._hit(
                    tender,
                    DuplicateStrategy.EXTERNAL_ID,
                    existing,
                    matched={"external_id": tender.external_id},
                )

        # --- stage 2: content hashes -----------------------------------
        if self.hash_enabled:
            clauses = []
            if self.hash_raw and raw_sha256:
                clauses.append(Tender.raw_sha256 == raw_sha256)
            if self.hash_text and text_sha256:
                clauses.append(Tender.text_sha256 == text_sha256)
            if clauses:
                row = session.execute(
                    select(Tender.id, Tender.raw_sha256).where(or_(*clauses)).limit(1)
                ).first()
                if row:
                    strategy = (
                        DuplicateStrategy.RAW_HASH
                        if raw_sha256 and row.raw_sha256 == raw_sha256
                        else DuplicateStrategy.TEXT_HASH
                    )
                    return self._hit(tender, strategy, row.id, matched={"hash": True})

        # --- stage 3: semantic -----------------------------------------
        if self.semantic_enabled:
            verdict = self._semantic_check(session, tender)
            if verdict.is_duplicate:
                return verdict

        return DuplicateVerdict(is_duplicate=False)

    # ------------------------------------------------------------------
    def _semantic_check(self, session: Session, tender: NormalizedTender) -> DuplicateVerdict:
        candidates = self._candidates(session, tender)
        if not candidates:
            return DuplicateVerdict(is_duplicate=False)

        backend = get_similarity_backend()
        needle = self.comparison_key(tender)
        if not needle:
            return DuplicateVerdict(is_duplicate=False)
        needle_vector = backend.encode(needle)

        best_id: uuid_module.UUID | None = None
        best_score = 0.0
        for candidate in candidates:
            cached = (candidate.dedup_vector or {}).get(backend.name)
            if cached:
                try:
                    score = backend.similarity_encoded(needle_vector, cached)
                except (NotImplementedError, TypeError, ValueError):
                    score = backend.similarity(needle, self.candidate_key(candidate))
            else:
                score = backend.similarity(needle, self.candidate_key(candidate))
            if score > best_score:
                best_score, best_id = score, candidate.id

        if best_id is not None and best_score >= self.threshold:
            return self._hit(
                tender,
                DuplicateStrategy.SEMANTIC,
                best_id,
                similarity=round(best_score, 4),
                matched={"threshold": self.threshold, "candidates": len(candidates)},
            )
        return DuplicateVerdict(is_duplicate=False, similarity=round(best_score, 4))

    def _candidates(self, session: Session, tender: NormalizedTender) -> list[Tender]:
        """Bounded candidate window — this is what keeps stage 3 affordable."""
        cutoff = utc_now() - timedelta(days=self.lookback_days)
        conditions = [Tender.created_at >= cutoff]

        if self.same_country and tender.country:
            conditions.append(or_(Tender.country == tender.country, Tender.country.is_(None)))

        if tender.deadline and self.deadline_tolerance_days >= 0:
            window = timedelta(days=self.deadline_tolerance_days)
            conditions.append(
                or_(
                    Tender.deadline.is_(None),
                    and_(
                        Tender.deadline >= tender.deadline - window,
                        Tender.deadline <= tender.deadline + window,
                    ),
                )
            )

        return list(
            session.execute(
                select(Tender)
                .where(*conditions)
                .order_by(desc(Tender.created_at))
                .limit(self.max_candidates)
            )
            .scalars()
            .all()
        )

    # ------------------------------------------------------------------
    def _hit(
        self,
        tender: NormalizedTender,
        strategy: DuplicateStrategy,
        canonical_id: uuid_module.UUID,
        *,
        similarity: float | None = None,
        matched: dict[str, Any] | None = None,
    ) -> DuplicateVerdict:
        duplicates_detected_total.labels(
            connector=tender.connector_key, strategy=strategy.value
        ).inc()
        logger.info(
            "dedup.rejected",
            connector=tender.connector_key,
            strategy=strategy.value,
            canonical_id=str(canonical_id),
            similarity=similarity,
            title=tender.title[:120],
        )
        return DuplicateVerdict(
            is_duplicate=True,
            strategy=strategy,
            canonical_id=canonical_id,
            similarity=similarity,
            matched_on=matched or {},
        )

    # ------------------------------------------------------------------
    def record_duplicate(
        self,
        session: Session,
        tender: NormalizedTender,
        verdict: DuplicateVerdict,
        *,
        job_id: uuid_module.UUID | None = None,
        raw_sha256: str | None = None,
        text_sha256: str | None = None,
    ) -> DuplicateRecord | None:
        """Persist the evidence and update the canonical tender's provenance."""
        if not self.record_evidence or not verdict.is_duplicate:
            return None

        record = DuplicateRecord(
            canonical_tender_id=verdict.canonical_id,
            strategy=(verdict.strategy or DuplicateStrategy.SEMANTIC).value,
            similarity=verdict.similarity,
            source_key=tender.connector_key,
            source_url=tender.source_url,
            canonical_url=self.canonicalize(tender.canonical_url or tender.source_url),
            raw_sha256=raw_sha256,
            text_sha256=text_sha256,
            title=tender.title[:1024],
            job_id=job_id,
            payload=tender.model_dump(mode="json", exclude_none=True),
        )
        session.add(record)

        if self.attach_source and verdict.canonical_id:
            canonical = session.get(Tender, verdict.canonical_id)
            if canonical is not None:
                canonical.duplicate_hits = (canonical.duplicate_hits or 0) + 1
                canonical.last_seen_at = utc_now()
                seen = list(canonical.seen_on_sources or [])
                if tender.connector_key not in seen:
                    # A notice visible on several portals is a stronger signal
                    # than one seen once — worth surfacing, not hiding.
                    seen.append(tender.connector_key)
                    canonical.seen_on_sources = seen
        return record
