"""Measure the end-to-end cost of producing a CV, on real stored data.

The parcours states an objective: an adapted CV in under fifteen seconds. This
walks the real path — load the validated shortlist, fetch the template from
object storage, read the CV, render — and times each leg, so the number is a
measurement rather than a claim.
"""
from __future__ import annotations

import time

from app.db.models.cv import CV
from app.db.models.shortlist import Shortlist
from app.db.models.template import DocumentTemplate
from app.db.models.tender import Tender
from app.db.session import session_scope
from app.services.generation import generate_cv
from app.services.storage import get_storage


def main() -> int:
    timings: dict[str, float] = {}

    start = time.perf_counter()
    with session_scope() as session:
        shortlist = (
            session.query(Shortlist).filter(Shortlist.status == "validated").first()
        )
        if shortlist is None:
            print("aucune shortlist validee — rien a mesurer")
            return 1
        retained = [e for e in shortlist.entries if e.decision == "retained"]
        if not retained:
            print("shortlist validee sans profil retenu")
            return 1
        entry = retained[0]
        tender = session.get(Tender, shortlist.tender_id) if shortlist.tender_id else None
        cv = session.get(CV, entry.cv_id)
        template_row = (
            session.query(DocumentTemplate)
            .filter(DocumentTemplate.kind == "cv", DocumentTemplate.is_active.is_(True))
            .first()
        )
        if template_row is None or cv is None:
            print("gabarit CV actif ou CV introuvable")
            return 1

        # Touch the deferred/lazy attributes inside the session.
        payload = {
            "criteria": dict(cv.criteria or {}),
            "structure": dict(cv.structure or {}),
            "display_name": cv.display_name,
            "headline": cv.headline,
            "original_filename": cv.original_filename,
        }
        entry_payload = {
            "label": entry.label,
            "rank": entry.rank,
            "score": entry.score,
            "decision": entry.decision,
            "evidence": list(entry.evidence or []),
        }
        tender_payload = (
            {
                "title": tender.title,
                "buyer": tender.buyer,
                "reference": tender.reference,
                "country": tender.country,
                "deadline": tender.deadline,
            }
            if tender is not None
            else {}
        )
        template_key = template_row.storage_key
        template_label = f"{template_row.key} v{template_row.version}"
    timings["lecture base"] = time.perf_counter() - start

    start = time.perf_counter()
    template_bytes = get_storage().get_bytes(template_key)
    timings["gabarit depuis MinIO"] = time.perf_counter() - start

    from types import SimpleNamespace

    start = time.perf_counter()
    document = generate_cv(
        template_bytes=template_bytes,
        cv=SimpleNamespace(**payload),
        entry=SimpleNamespace(**entry_payload),
        tender=SimpleNamespace(**tender_payload) if tender_payload else None,
    )
    timings["rendu docx"] = time.perf_counter() - start

    total = sum(timings.values())
    print()
    print(f"profil     {entry_payload['label'][:50]}")
    print(f"AO         {(tender_payload.get('title') or '')[:60]}")
    print(f"gabarit    {template_label}")
    print(f"experiences {len(payload['structure'].get('experiences') or [])} · "
          f"formations {len(payload['structure'].get('formations') or [])}")
    print()
    for label, seconds in timings.items():
        print(f"  {label:<24} {seconds * 1000:>8.1f} ms")
    print(f"  {'TOTAL':<24} {total * 1000:>8.1f} ms   (objectif < 15 000 ms)")
    print()
    print(f"fichier    {document.filename}  ({len(document.content):,} octets)")
    if document.empty_fields:
        print(f"champs vides {', '.join(document.empty_fields)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
