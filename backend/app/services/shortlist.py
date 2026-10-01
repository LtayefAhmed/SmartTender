"""Turning a matching run into a decision a human can be held to.

Everything here is a pure function over ORM objects: nothing opens a session,
nothing commits. The router owns the transaction, and these rules can be tested
against plain objects without a database — which is what keeps them tested at
all, since a rule that needs infrastructure to exercise is a rule that quietly
stops being exercised.

Three decisions are baked in, and each of them could reasonably have gone the
other way:

**A capture keeps the refused profiles too.** A shortlist showing only its
winners cannot be argued with. "We looked at 462 people, 244 were ruled out by
the technology veto, here are eight of them" is a stronger statement than a
top-20 with no denominator.

**Validation never invents a decision.** It would be convenient to mark every
untouched row as rejected when the list is locked — one click instead of
twenty. It would also write "rejected by Ahmed" against profiles Ahmed never
opened. A silence stays a silence: ``PENDING`` means *not selected and not
examined*, which is a different fact from *examined and turned down*, and
Phase 8 will want to tell them apart.

**Validating an empty selection is allowed.** "We looked and nobody fits" is a
real conclusion and worth recording — it is the same information as a loss when
tuning relevance later. Generation refuses it; capture does not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.core.enums import ShortlistDecision, ShortlistStatus
from app.core.identity import utc_now
from app.core.logging import get_logger
from app.db.models.shortlist import Shortlist, ShortlistEntry

logger = get_logger(__name__)

__all__ = [
    "ShortlistLocked",
    "apply_decision",
    "build_shortlist",
    "retained_entries",
    "summarise",
    "validate_shortlist",
]


class ShortlistLocked(Exception):
    """Raised when a validated capture is edited.

    Validation is the human lock the parcours calls for. Letting a decision be
    changed afterwards, without a trace, would make the lock decorative: the
    generated documents would no longer correspond to anything anyone signed.
    A new capture is the supported way to change one's mind.
    """


def build_shortlist(
    outcome: dict[str, Any],
    *,
    tenant: str,
    created_by: str | None = None,
    label: str | None = None,
) -> Shortlist:
    """Freeze one matching run.

    ``outcome`` is what ``rank_tender_candidates`` returned. It is read
    defensively — every field with a default — because the task's shape is
    allowed to grow, and a capture that raises on an unfamiliar key would take
    the whole feature down for an addition that concerns it not at all.
    """
    shortlist = Shortlist(
        tenant_id=tenant,
        tender_id=_as_uuid(outcome.get("tender_id")),
        tender_title=(outcome.get("title") or None),
        label=label,
        status=ShortlistStatus.OPEN.value,
        requirements=list(outcome.get("requirements") or []),
        required_technologies=[str(t) for t in (outcome.get("required_technologies") or [])],
        structured_requirements=outcome.get("structured_requirements"),
        weights=dict(outcome.get("weights") or {}),
        kept_total=int(outcome.get("kept_total") or 0),
        vetoed_total=int(outcome.get("vetoed_total") or 0),
        created_by=created_by,
    )

    for position, candidate in enumerate(outcome.get("candidates") or [], start=1):
        shortlist.entries.append(_build_entry(candidate, rank=position))

    logger.info(
        "shortlist.captured",
        tender_uuid=outcome.get("tender_id"),
        entries=len(shortlist.entries),
        kept_total=shortlist.kept_total,
        vetoed_total=shortlist.vetoed_total,
    )
    return shortlist


def _build_entry(candidate: dict[str, Any], *, rank: int) -> ShortlistEntry:
    return ShortlistEntry(
        cv_id=_as_uuid(candidate.get("cv_id")),
        rank=rank,
        # The label as the validator read it. A CV re-imported later under a
        # different display name must not rewrite what was on screen.
        label=str(
            candidate.get("label")
            or candidate.get("display_name")
            or candidate.get("filename")
            or ""
        )[:512],
        filename=(str(candidate["filename"])[:512] if candidate.get("filename") else None),
        score=float(candidate.get("score") or 0.0),
        similarity=float(candidate.get("similarity") or 0.0),
        coverage=float(candidate.get("coverage") or 0.0),
        technology_ratio=float(candidate.get("technology_ratio") or 0.0),
        matched_technologies=[str(t) for t in (candidate.get("matched_technologies") or [])],
        missing_technologies=[str(t) for t in (candidate.get("missing_technologies") or [])],
        vetoed=bool(candidate.get("vetoed")),
        veto_reason=(
            str(candidate["veto_reason"])[:512] if candidate.get("veto_reason") else None
        ),
        evidence=list(candidate.get("evidence") or []),
        decision=ShortlistDecision.PENDING.value,
    )


def apply_decision(
    entry: ShortlistEntry,
    *,
    decision: ShortlistDecision | str,
    actor: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> ShortlistEntry:
    """Record what a human decided about one candidate.

    Retaining a vetoed profile is permitted, and deliberately so. The veto is a
    lexical rule over a technology list read out of a document; it is right far
    more often than not, but a bid manager who knows the consultant is the
    authority here. What matters is that the override is *visible*: the entry
    keeps ``vetoed=True`` alongside its retention, so the record shows a human
    went against the ranking rather than hiding that they did.
    """
    if entry.shortlist is not None and entry.shortlist.status == ShortlistStatus.VALIDATED.value:
        raise ShortlistLocked(
            "This shortlist is validated. Capture a new one to change the selection."
        )

    resolved = ShortlistDecision(decision) if isinstance(decision, str) else decision
    entry.decision = resolved.value
    entry.decided_by = actor
    entry.decided_at = now or utc_now()
    if note is not None:
        entry.decision_note = note

    if resolved is ShortlistDecision.RETAINED and entry.vetoed:
        logger.info(
            "shortlist.veto_overridden",
            entry_uuid=str(entry.id),
            reason=entry.veto_reason,
            actor=actor,
        )
    return entry


def validate_shortlist(
    shortlist: Shortlist,
    *,
    actor: str | None = None,
    now: datetime | None = None,
) -> Shortlist:
    """Lock the selection. Past here it is an input, not a working document."""
    if shortlist.status == ShortlistStatus.VALIDATED.value:
        raise ShortlistLocked("This shortlist is already validated.")

    shortlist.status = ShortlistStatus.VALIDATED.value
    shortlist.validated_by = actor
    shortlist.validated_at = now or utc_now()

    kept = retained_entries(shortlist)
    logger.info(
        "shortlist.validated",
        shortlist_uuid=str(shortlist.id),
        retained=len(kept),
        # Counted because it is the interesting number: profiles nobody ruled
        # on. A validation with twenty of these is a list that was skimmed.
        pending=sum(
            1 for e in shortlist.entries if e.decision == ShortlistDecision.PENDING.value
        ),
        actor=actor,
    )
    return shortlist


def retained_entries(shortlist: Shortlist) -> list[ShortlistEntry]:
    """The profiles a human explicitly kept — the input to generation.

    Explicitly: ``PENDING`` never counts. Generating a bailleur CV for someone
    nobody selected would put a real person's name on a real submission on the
    strength of a rank.
    """
    return [
        entry
        for entry in shortlist.entries
        if entry.decision == ShortlistDecision.RETAINED.value
    ]


def summarise(shortlist: Shortlist, *, include_entries: bool = True) -> dict[str, Any]:
    """The API shape."""
    counts = {value.value: 0 for value in ShortlistDecision}
    for entry in shortlist.entries:
        counts[entry.decision] = counts.get(entry.decision, 0) + 1

    payload: dict[str, Any] = {
        "id": str(shortlist.id),
        "tender_id": str(shortlist.tender_id) if shortlist.tender_id else None,
        "tender_title": shortlist.tender_title,
        "label": shortlist.label,
        "status": shortlist.status,
        "required_technologies": list(shortlist.required_technologies or []),
        "structured_requirements": shortlist.structured_requirements,
        "weights": dict(shortlist.weights or {}),
        # Over everything considered, not over the stored slice.
        "kept_total": shortlist.kept_total,
        "vetoed_total": shortlist.vetoed_total,
        "decisions": counts,
        "created_by": shortlist.created_by,
        "created_at": shortlist.created_at.isoformat() if shortlist.created_at else None,
        "validated_by": shortlist.validated_by,
        "validated_at": (
            shortlist.validated_at.isoformat() if shortlist.validated_at else None
        ),
        "note": shortlist.note,
    }
    if include_entries:
        payload["requirements"] = list(shortlist.requirements or [])
        payload["entries"] = [_entry_dict(entry) for entry in shortlist.entries]
    return payload


def _entry_dict(entry: ShortlistEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "cv_id": str(entry.cv_id) if entry.cv_id else None,
        "rank": entry.rank,
        "label": entry.label,
        "filename": entry.filename,
        "score": round(entry.score, 4),
        "similarity": round(entry.similarity, 4),
        "coverage": round(entry.coverage, 4),
        "technology_ratio": round(entry.technology_ratio, 4),
        "matched_technologies": list(entry.matched_technologies or []),
        "missing_technologies": list(entry.missing_technologies or []),
        "vetoed": entry.vetoed,
        "veto_reason": entry.veto_reason,
        "evidence": list(entry.evidence or []),
        "decision": entry.decision,
        "decided_by": entry.decided_by,
        "decided_at": entry.decided_at.isoformat() if entry.decided_at else None,
        "decision_note": entry.decision_note,
    }


def _as_uuid(value: Any) -> Any:
    import uuid as uuid_module

    if isinstance(value, uuid_module.UUID):
        return value
    if not value:
        return None
    try:
        return uuid_module.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
