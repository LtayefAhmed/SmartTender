"""Offline synthetic demo using SmartTender's actual lexical implementation.

Run with the backend installed. No database, Redis, storage, model or network.
This demonstrates scoring, not PDF ingestion or the full Celery request flow.
"""

import json

from app.core.identity import normalize_text
from app.services.similarity import LexicalBackend


def main():
    job = "Python developer building REST APIs with SQL and Docker."
    technologies = ["Python", "SQL", "Docker"]
    candidates = {
        "A - Backend developer": "Python developer building REST APIs with SQL and Docker. Developed backend services and automated tests.",
        "B - Data analyst": "Data analyst using Python and SQL for reports, dashboards and data cleaning.",
        "C - Graphic designer": "Graphic designer creating logos and illustrations with Photoshop and Illustrator.",
    }
    backend = LexicalBackend()
    results = []
    for label, text in candidates.items():
        normalized = normalize_text(text)
        found = [tech for tech in technologies if normalize_text(tech) in normalized]
        text_score = backend.similarity(job, text)
        coverage = len(found) / len(technologies)
        results.append({
            "candidate": label,
            "text_similarity": round(text_score, 4),
            "technology_coverage": round(coverage, 4),
            "score": round(0.75 * text_score + 0.25 * coverage, 4),
            "matched": found,
            "missing": [tech for tech in technologies if tech not in found],
        })
    results.sort(key=lambda item: item["score"], reverse=True)
    print(json.dumps({"backend": backend.name, "job": job, "technologies": technologies, "candidates": results}, indent=2))


if __name__ == "__main__":
    main()
