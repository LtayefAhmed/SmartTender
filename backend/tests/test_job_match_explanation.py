from contextlib import contextmanager
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("technologies,weight", [([], 1.0), (["Python", "Java"], 0.75)])
def test_explanation_matches_score_and_evidence(monkeypatch, technologies, weight):
    from app.db import session as sessions
    from app.services import extraction, similarity, storage
    from app.workers.tasks.job_match import rank_job_posting_candidates

    row = SimpleNamespace(id="cv-1", storage_key="key", content_type="application/pdf", original_filename="cv.pdf", source_url=None)
    rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [row]))

    @contextmanager
    def session_scope():
        yield SimpleNamespace(execute=lambda query: rows)

    monkeypatch.setattr(sessions, "session_scope", session_scope)
    monkeypatch.setattr(storage, "get_storage", lambda: SimpleNamespace(get_bytes=lambda key: b"pdf"))
    text = "contact " * 80 + "Python developer SQL APIs"
    monkeypatch.setattr(extraction, "get_extractor", lambda: SimpleNamespace(extract=lambda *a, **kw: SimpleNamespace(text=text)))
    monkeypatch.setattr(similarity, "get_similarity_backend", lambda: similarity.LexicalBackend())
    result = rank_job_posting_candidates.run(job_text="Python developer SQL APIs", tenant="test", filters={"technologies": technologies})
    candidate = result["candidates"][0]
    explanation = candidate["explanation"]
    expected = explanation["text_similarity"] * weight
    if technologies:
        expected += explanation["technology_coverage"] * 0.25
        assert candidate["matched_technologies"] == ["Python"]
        assert candidate["missing_technologies"] == ["Java"]
    assert candidate["score"] == pytest.approx(expected, abs=0.0001)
    assert explanation["rank"] == 1
    assert explanation["total_candidates"] == 1
    assert "Python" in candidate["evidence"][0]["passage"]


def test_filters_apply_before_limit_and_totals_cover_corpus(monkeypatch):
    from app.db import session as sessions
    from app.services import extraction, similarity, storage
    from app.workers.tasks.job_match import rank_job_posting_candidates

    texts = {"high": "Python\n2 years of experience", "low": "SQL developer\n7 years of experience", "unknown": "Python"}
    rows = [SimpleNamespace(id=key, storage_key=key, content_type="application/pdf", original_filename=key + ".pdf", source_url=None) for key in texts]

    @contextmanager
    def session_scope():
        yield SimpleNamespace(execute=lambda query: SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows)))

    monkeypatch.setattr(sessions, "session_scope", session_scope)
    monkeypatch.setattr(storage, "get_storage", lambda: SimpleNamespace(get_bytes=lambda key: texts[key]))
    monkeypatch.setattr(extraction, "get_extractor", lambda: SimpleNamespace(extract=lambda data, **kw: SimpleNamespace(text=data)))
    monkeypatch.setattr(similarity, "get_similarity_backend", lambda: similarity.LexicalBackend())
    result = rank_job_posting_candidates.run(job_text="Python", tenant="test", filters={"min_experience_years": 5}, limit=1)
    assert result["candidates"][0]["cv_id"] == "low"
    assert result["candidates"][0]["filter_status"] == "pass"
    assert result["candidates"][0]["filter_checks"][0]["evidence"] == ["7 years of experience"]
    assert (result["kept_total"], result["unverified_total"], result["filtered_total"], result["total_candidates"]) == (1, 1, 1, 3)
    all_results = rank_job_posting_candidates.run(job_text="Python", tenant="test", filters={"min_experience_years": 5})
    assert [c["filter_status"] for c in all_results["candidates"]] == ["pass", "unknown", "fail"]
    assert all(c["filtered_out"] for c in all_results["candidates"][1:])
