"""Conservative local CV criteria checks with verbatim evidence; no model calls.

Languages and certifications are all-of requirements. Education is any-of.
Absent/ambiguous statements remain unknown, never evidence of qualification.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.core.identity import normalize_text

LANGUAGES = {
    "Français": ["francais", "french"], "Anglais": ["anglais", "english"],
    "Arabe": ["arabe", "arabic"], "Allemand": ["allemand", "german", "deutsch"],
    "Espagnol": ["espagnol", "spanish", "espanol"], "Italien": ["italien", "italian"],
}
CERTIFICATIONS = {
    "PMP": ["pmp", "project management professional"],
    "Prince2": ["prince2", "prince 2"], "ITIL": ["itil"],
    "Scrum Master": ["scrum master", "psm", "csm", "professional scrum master", "certified scrum master"],
    "PMI-ACP": ["pmi acp"], "SAFe Agilist": ["safe agilist"],
    "AWS Certified": ["aws certified", "certification aws", "certifie aws"],
    "Azure Fundamentals": ["azure fundamentals", "az 900"],
    "Google Cloud Certified": ["google cloud certified", "gcp certified"],
    "CISSP": ["cissp"], "CISA": ["cisa"], "TOGAF": ["togaf"],
    "ISO 27001": ["iso 27001", "iso27001"],
}
EDUCATION = {
    "Master": ["master", "masters", "master s", "msc", "m sc"],
    "Ingénieur": ["diplome d ingenieur", "engineering degree", "ingenieur"],
    "Licence": ["licence", "bachelor", "bachelors", "bachelor s", "bsc", "b sc"],
    "Doctorat": ["doctorat", "phd", "ph d", "doctorate"],
    "Bac+5": ["bac 5"], "Bac+3": ["bac 3"], "BTS": ["bts"],
}
HEADINGS = {
    "experience": {"experience", "experiences", "experience professionnelle", "experiences professionnelles", "professional experience", "work experience", "employment", "employment history"},
    "education": {"education", "formation", "formations", "diplomes", "academic background", "qualifications"},
    "certifications": {"certifications", "certification", "certificates", "certificats", "habilitations"},
    "languages": {"languages", "language", "langues", "langue"},
    "other": {"skills", "competences", "projects", "projets", "interests", "loisirs", "summary", "profil", "profile", "contact"},
}
UNCERTAIN = re.compile(r"\b(en cours|in progress|preparing|preparation|preparing for|planned|planifie|expected|candidate for|aspiring|expired|expire|expiree|not certified|non certifie|sans certification|not completed|non obtenu|dropped out|training|course)\b")


def _contains(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(normalize_text(phrase)) + r"(?!\w)", text))


def _lines(text: str) -> list[tuple[str, str, str]]:
    section = ""
    rows = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        norm = normalize_text(raw)
        for name, titles in HEADINGS.items():
            if norm in titles or normalize_text(raw.split(":", 1)[0]) in titles:
                section = name
                break
        rows.append((raw, norm, section))
    return rows


def _aliases(value: str, vocabulary: dict[str, list[str]]) -> list[str]:
    wanted = normalize_text(value)
    for label, variants in vocabulary.items():
        if wanted in [normalize_text(label), *variants]:
            return variants
    return [wanted]


def _fact(rows: list, kind: str, value: str, vocabulary: dict) -> dict[str, Any]:
    variants = _aliases(value, vocabulary)
    uncertain = []
    positive = []
    for index, (raw, norm, section) in enumerate(rows):
        if not any(_contains(norm, alias) for alias in variants):
            continue
        # Avoid job titles, courses and company standards becoming qualifications.
        if kind == "education":
            contextual = section == kind or bool(re.search(r"\b(degree|diplome|graduated|obtained|titulaire|universit\w*|bachelor|msc|phd)\b", norm))
            if "scrum master" in norm:
                contextual = False
        elif kind == "certifications":
            contextual = section == kind or bool(re.search(r"\b(certified|certifie\w*|certification\w*|credential\w*)\b", norm))
            if re.search(r"\b(company|entreprise|organisation|organization)\b", norm):
                contextual = False
        else:
            contextual = section == kind or bool(re.search(r"\b(fluent|native|bilingual|courant|maternelle|bilingue|spoken|speaks|parle|niveau|proficiency|language|langue|a1|a2|b1|b2|c1|c2)\b", norm))
        nearby = [r for r in rows[max(0, index - 1):index] + rows[index + 1:index + 2]
                  if r[2] == section and re.match(r"^(in progress|en cours|expected|expired|expire|preparation)(?:\b|\s)", r[1])]
        negative = bool(re.search(r"\b(no|not|sans|aucun|aucune|pas de|ne parle pas)\b", norm))
        if contextual and (UNCERTAIN.search(norm) or negative or nearby):
            uncertain.append(raw)
            uncertain.extend(r[0] for r in nearby)
        elif contextual:
            positive.append(raw)
    if positive and not uncertain:
        return {"status": "pass", "evidence": positive[:2], "reason": "Mention explicite dans le CV."}
    return {"status": "unknown", "evidence": uncertain[:2], "reason": "Mention ambiguë, en cours ou non confirmée." if uncertain else "Aucune mention explicite vérifiable dans le CV."}


def _experience(rows: list, today: date) -> dict[str, Any]:
    claims = []
    for raw, norm, section in rows:
        if UNCERTAIN.search(norm) or re.search(r"\b(required|minimum requis|requis|recherche|seeking|not|no|pas)\b", norm):
            continue
        # Retain decimal punctuation for durations, but normalize accents/case.
        numeric = normalize_text(raw, strip_punctuation=False)
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*\+?\s*(?:years?|ans?|annees?)\s+(?:(?:of|d['’]?)\s*)?(?:professional\s+|professionnelle\s+)?experience", numeric)
        if not match:
            match = re.search(r"experience\s*[:=]?\s*(\d+(?:[.,]\d+)?)\s*\+?\s*(?:years?|ans?|annees?)\b", numeric)
        if match:
            value = float(match.group(1).replace(",", "."))
            if 0 <= value <= 60:
                claims.append((value, raw))
    if claims:
        distinct = {value for value, _ in claims}
        if len(distinct) > 1:
            return {"years": None, "evidence": [raw for _, raw in claims], "method": "conflicting", "reason": "Durées déclarées différentes : vérification nécessaire."}
        value, raw = claims[0]
        lower_bound = bool(re.search(r"\+|\b(over|more than|at least|au moins|plus de)\b", normalize_text(raw, strip_punctuation=False)))
        return {"years": value, "evidence": [raw], "method": "stated_lower_bound" if lower_bound else "stated", "reason": "Durée minimale déclarée dans le CV." if lower_bound else "Durée déclarée dans le CV."}

    # Conservative lower bound from explicitly dated employment entries only.
    # Dates without months: December for start, January for end.
    date_pattern = r"(?:(0?[1-9]|1[0-2])[/.-])?((?:19|20)\d{2})\s*(?:-|–|—|to|a|au)\s*(?:(?:(0?[1-9]|1[0-2])[/.-])?((?:19|20)\d{2})|(present|current|aujourd['’]?hui|actuel|en cours))"
    intervals = []
    evidence = []
    now = today.year * 12 + today.month - 1
    for raw, norm, section in rows:
        if section != "experience":
            continue
        dated = normalize_text(raw, strip_punctuation=False)
        months = ["jan(?:uary|vier)?", "feb(?:ruary)?|fev(?:rier)?", "mar(?:ch|s)?", "apr(?:il)?|avr(?:il)?", "may|mai", "jun(?:e)?|juin", "jul(?:y)?|juil(?:let)?", "aug(?:ust)?|aout", "sep(?:t(?:ember|embre)?)?", "oct(?:ober|obre)?", "nov(?:ember|embre)?", "dec(?:ember|embre)?"]
        for month, names in enumerate(months, 1):
            dated = re.sub(r"\b(?:" + names + r")\.?\s+(?=(?:19|20)\d{2})", f"{month:02d}/", dated)
        for match in re.finditer(date_pattern, dated):
            sm, sy, em, ey, ongoing = match.groups()
            start = int(sy) * 12 + (int(sm) if sm else 12) - 1
            end = now if ongoing else int(ey) * 12 + (int(em) if em else 1) - 1
            if start < end <= now:
                intervals.append((start, end))
                evidence.append(raw)
    merged: list[list[int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return {"years": sum(end - start for start, end in merged) / 12 if merged else None,
            "evidence": evidence, "method": "dates_lower_bound" if merged else "unavailable",
            "reason": "Durée minimale calculée sur les périodes professionnelles, sans double compter les chevauchements." if merged else "Durée d'expérience non vérifiable dans le CV."}


def evaluate_cv_filters(text: str, filters: dict[str, Any], *, today: date | None = None) -> dict[str, Any]:
    rows = _lines(text)
    today = today or date.today()
    experience = _experience(rows, today)
    checks = []
    ages = []
    for raw, norm, section in rows:
        value = None
        stated = re.search(r"\b(?:age|aged)\s*:?\s*(\d{1,3})\b|\b(\d{1,3})\s+years?\s+old\b", normalize_text(raw, strip_punctuation=False))
        if stated:
            value = int(stated.group(1) or stated.group(2))
        elif re.search(r"\b(date de naissance|ne le|nee le|birth date|date of birth|dob|born)\b", norm):
            birthday = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b|\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})\b", raw)
            if birthday:
                try:
                    birth = date(*map(int, birthday.group(1, 2, 3))) if birthday.group(1) else date(int(birthday.group(6)), int(birthday.group(5)), int(birthday.group(4)))
                    value = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
                except ValueError:
                    pass
        if value is not None and 0 <= value <= 120:
            ages.append((value, raw))
    age = ages[0][0] if ages and len({value for value, _ in ages}) == 1 else None
    age_min, age_max = filters.get("age_min"), filters.get("age_max")
    if age_min is not None or age_max is not None:
        age_status = "unknown" if age is None else "fail" if (age_min is not None and age < age_min) or (age_max is not None and age > age_max) else "pass"
        checks.append({"criterion": "age", "requested": f"Âge : {age_min if age_min is not None else 0}–{age_max if age_max is not None else 120} ans",
                       "status": age_status, "observed": age, "evidence": [raw for _, raw in ages],
                       "reason": "Âge déclaré ou calculé depuis une date de naissance complète (jour/mois/année ou ISO)." if age is not None else "Âge absent ou contradictoire : vérification nécessaire."})
    minimum = filters.get("min_experience_years")
    if minimum is not None:
        years = experience["years"]
        status = "unknown" if years is None else "pass" if years >= minimum else "fail" if experience["method"] == "stated" else "unknown"
        checks.append({"criterion": "experience", "requested": f"{minimum} ans minimum", "status": status,
                       "observed": round(years, 2) if years is not None else None,
                       "evidence": experience["evidence"], "reason": experience["reason"], "method": experience["method"]})
    extracted = {}
    for kind, vocabulary in (("languages", LANGUAGES), ("certifications", CERTIFICATIONS), ("education", EDUCATION)):
        values = list(dict.fromkeys(str(v).strip() for v in filters.get(kind, []) if str(v).strip()))
        extracted[kind] = [label for label in vocabulary if _fact(rows, kind, label, vocabulary)["status"] == "pass"]
        specific = [{"criterion": kind, "requested": value, **_fact(rows, kind, value, vocabulary)} for value in values]
        if kind == "education" and specific:
            passed = next((check for check in specific if check["status"] == "pass"), None)
            checks.append({"criterion": kind, "requested": " ou ".join(values),
                           **({k: passed[k] for k in ("status", "evidence", "reason")} if passed else
                              {"status": "unknown", "evidence": [e for check in specific for e in check["evidence"]], "reason": "Aucun des diplômes demandés n'est confirmé."})})
        else:
            checks.extend(specific)
        extracted[kind].extend(check["requested"] for check in specific if check["status"] == "pass" and check["requested"] not in extracted[kind])
    status = "fail" if any(c["status"] == "fail" for c in checks) else "unknown" if any(c["status"] == "unknown" for c in checks) else "pass"
    return {"status": status, "checks": checks,
            "profile": {"age": age, "experience_years": round(experience["years"], 2) if experience["years"] is not None else None,
                        "education": ", ".join(extracted["education"]) or None,
                        "languages": extracted["languages"], "certifications": extracted["certifications"], "skills": []}}
