"""Measure whether the ranking discriminates, not merely whether it orders.

Three Inetum CVs can show that the matcher puts consultants in some sequence.
They cannot show that it would refuse an accountant, because there is no
accountant to refuse. Without candidates that *must not* match, a ranking is
unfalsifiable: everything it returns looks plausible because everything it was
given was plausible.

So this scores a real tender against a corpus that deliberately contains
people who have no business being on the shortlist, and reports the two
numbers that matter:

    separation   how far the in-domain median sits above the out-of-domain one
    veto rate    how many out-of-domain candidates were floored to zero

Category comes from the CV text rather than from metadata: these resumes open
with their job title in capitals, and the folder name is lost on import.

Run inside the AI worker::

    docker compose -f ../docker-compose.yml exec -T worker-ai \n        python scripts/evaluer_matching.py
"""

from __future__ import annotations

import statistics
import sys

#: Tender to rank against. The richest dossier in the corpus — a .7z holding
#: CCTP, CCAP, BPU and the development guidelines — and the only one naming a
#: full stack, which is what makes the technology lock testable at all.
TENDER_ID = "7fd4ebf0-4e43-4345-8cbd-7eadffb229fa"

#: Job titles that open these resumes. In-domain candidates should rank; the
#: others exist to be refused.
IN_DOMAIN = ("INFORMATION TECHNOLOGY", "IT MANAGEMENT", "SYSTEMS", "ENGINEER", "DEVELOPER")
OUT_OF_DOMAIN = ("ACCOUNTANT", "ACCOUNTING", "ARTS", "CHEF", "FINANCE", "ADVOCATE")


def _category(text: str) -> str:
    head = (text or "")[:120].upper()
    if any(marker in head for marker in OUT_OF_DOMAIN):
        return "hors domaine"
    if any(marker in head for marker in IN_DOMAIN):
        return "informatique"
    return "indetermine"


def main() -> int:
    from sqlalchemy import text as sql

    from app.db.session import session_scope
    from app.services.chunking import chunk_text
    from app.services.matching import extract_requirements, match_tender, required_technologies

    with session_scope() as session:
        row = session.execute(
            sql(
                "select title, coalesce(description,''), coalesce(extracted_text,'') "
                "from tenders where id = :identifier"
            ),
            {"identifier": TENDER_ID},
        ).one_or_none()
        if row is None:
            print(f"Tender {TENDER_ID} not found.", file=sys.stderr)
            return 1
        title, description, extracted = row

        categories = {
            str(identifier): _category(text)
            for identifier, text in session.execute(
                sql("select id, extracted_text from cvs where extraction_status = 'extracted'")
            ).all()
        }

    tender_text = f"{description}\n\n{extracted}"
    requirements = extract_requirements(
        [(c.text, c.document, c.index, c.priority) for c in chunk_text(tender_text)], limit=15
    )
    wanted = required_technologies(tender_text)

    print(f"OFFRE      {title[:66]}")
    print(f"exigences  {len(requirements)} passages · {len(wanted)} technologies")
    print(f"corpus     {len(categories)} CV lus")
    for name in ("informatique", "hors domaine", "indetermine"):
        print(f"           {sum(1 for v in categories.values() if v == name):>4}  {name}")

    # `limit` well above the corpus size: a shortlist would hide exactly the
    # thing under test, which is where the out-of-domain candidates land.
    matches = match_tender(
        tender_text=tender_text, requirements=requirements, tenant="default", limit=400
    )
    print(f"\n{len(matches)} candidats ont au moins un passage pertinent\n")

    scores: dict[str, list[float]] = {"informatique": [], "hors domaine": [], "indetermine": []}
    vetoed: dict[str, int] = dict.fromkeys(scores, 0)
    for match in matches:
        category = categories.get(match.cv_id, "indetermine")
        scores[category].append(match.score)
        if match.vetoed:
            vetoed[category] += 1

    print(f"{'categorie':<14} {'n':>4} {'median':>8} {'max':>8} {'vetos':>7}")
    for category, values in scores.items():
        if not values:
            continue
        print(
            f"{category:<14} {len(values):>4} {statistics.median(values):>8.3f} "
            f"{max(values):>8.3f} {vetoed[category]:>6}"
        )

    it_scores = scores["informatique"]
    out_scores = scores["hors domaine"]
    if it_scores and out_scores:
        gap = statistics.median(it_scores) - statistics.median(out_scores)
        print(f"\nSEPARATION des medianes : {gap:+.3f}")
        rate = vetoed["hors domaine"] / len(out_scores)
        print(f"VETO sur les hors domaine : {rate:.0%}")

    print("\n--- tete de classement ---")
    for match in matches[:8]:
        flag = "VETO " if match.vetoed else "     "
        category = categories.get(match.cv_id, "?")
        print(
            f"{match.score:.3f} {flag}{category:<13} {match.filename[:24]:<26} "
            f"technos {match.technology_ratio:.0%} {match.matched_technologies[:4]}"
        )

    print("\n--- fond de classement ---")
    for match in matches[-5:]:
        flag = "VETO " if match.vetoed else "     "
        category = categories.get(match.cv_id, "?")
        print(f"{match.score:.3f} {flag}{category:<13} {match.filename[:24]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
