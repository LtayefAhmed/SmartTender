"""Ranking candidates, and the rules that keep a ranking honest.

Written after a measurement, not before one. Searching the real corpus with the
embedding model alone gave a true requirement at 0.315 and a deliberately
unrelated control at 0.270 — forty-five thousandths apart, with a CV's
letterhead as the top hit for a query about authentication. Cosine similarity
alone cannot carry this decision, and these tests pin the three rules that
carry it instead: the veto, coverage, and evidence.
"""

from __future__ import annotations

import pytest

from app.services.matching import (
    CandidateMatch,
    MatchWeights,
    Requirement,
    extract_requirements,
    required_technologies,
    technology_lexicon,
)


class TestTheLexiconAnswersItsOwnQuestion:
    """Kept apart from ``scoring.yaml`` on purpose. That file answers "is this
    tender ours" and holds service categories; this one answers "what stack
    does it need"."""

    def test_it_recognises_a_real_stack(self):
        found = required_technologies(
            "Prestations de developpement en Symfony et PHP 8, conteneurisees "
            "avec Docker et Kubernetes, integration continue via GitLab."
        )

        assert {"Symfony", "PHP", "Docker", "Kubernetes", "GitLab"} <= set(found)

    def test_a_service_category_is_not_a_technology(self):
        """The bug this file exists to fix: a consultation demanding Symfony,
        PHP, Docker, Kubernetes, GitLab and SonarQube matched only "monitoring"
        and "DevOps" when locked against the scoring catalogue."""
        found = required_technologies(
            "Tierce maintenance applicative du portail, avec assistance technique."
        )

        assert "Symfony" not in found
        assert "portail" not in found

    def test_a_short_term_does_not_fire_inside_a_word(self):
        """`.NET` inside a buyer named "Khazanet" put a waste-collection
        contract at the top of the dashboard. The same trap applies here."""
        assert ".NET" not in required_technologies("Marche attribue a la societe Khazanet")
        assert "Go" not in required_technologies("Les prestations gouvernementales")

    def test_the_lexicon_is_loaded_and_non_trivial(self):
        assert len(technology_lexicon()) > 50


class TestRequirementSelectionIgnoresFurniture:
    @staticmethod
    def _passage(text: str, priority: int = 0, position: int = 0):
        return (text, "CCTP.pdf", position, priority)

    def test_a_letterhead_is_never_a_requirement(self):
        """Measured on a real CV: "inetum.com Stories, 5-7 rue Touzet Gaillard,
        93400 Saint-Ouen" came top of a search for authentication and tokens,
        ahead of the passage listing SSO, SAML and OAuth. A block naming no
        skill must not become evidence for one."""
        passages = [
            self._passage("www.exemple.fr 5-7 rue Touzet Gaillard 93400 Saint-Ouen 01 23 45 67"),
            self._passage(
                "Le titulaire doit disposer d'une experience confirmee en developpement "
                "Symfony et maitriser les pratiques d'integration continue sur GitLab.",
                position=1,
            ),
        ]

        chosen = extract_requirements(passages)

        assert len(chosen) == 1
        assert "Symfony" in chosen[0].text

    def test_obligation_language_ranks_first(self):
        passages = [
            self._passage("Le marche porte sur la refonte du systeme d'information. " * 3),
            self._passage(
                "Le candidat doit justifier d'une experience exigee de cinq ans et "
                "maitriser les competences requises en architecture microservices.",
                position=1,
            ),
        ]

        chosen = extract_requirements(passages, limit=2)

        assert "doit justifier" in chosen[0].text

    def test_a_substantive_document_outranks_a_form(self):
        body = "Le candidat doit maitriser les exigences de securite applicative. " * 2
        passages = [(body, "DC1.doc", 0, 1), (body, "CCTP.pdf", 1, 0)]

        chosen = extract_requirements(passages, limit=2)

        assert chosen[0].document == "CCTP.pdf"

    def test_selection_is_bounded(self):
        """One vector search per requirement: a 290-passage dossier searched in
        full would cost 290 queries to bury the few that constrain who bids."""
        passages = [
            self._passage(
                f"Le titulaire doit assurer la prestation numero {index} avec les "
                "competences requises et l'experience exigee sur ce lot, dans le "
                "respect des delais contractuels et des niveaux de service definis "
                "au cahier des clauses techniques particulieres.",
                position=index,
            )
            for index in range(80)
        ]

        assert len(extract_requirements(passages, limit=15)) == 15


class TestTheVetoIsNotANegotiation:
    """The anti-bias rule from the specification: a well-written CV without the
    required skills collapses rather than charming the ranking."""

    def test_a_candidate_evidencing_nothing_scores_zero(self, monkeypatch):
        matches = _run_match(
            monkeypatch,
            tender_text="Developpement Symfony et PHP, conteneurise avec Docker.",
            cv_text="Comptable generaliste, consolidation des bilans, fiscalite et audit.",
        )

        assert matches[0].vetoed is True
        assert matches[0].score == 0.0
        assert "Symfony" in matches[0].missing_technologies

    def test_evidencing_one_required_technology_lifts_the_veto(self, monkeypatch):
        """The veto is a floor, not a bar: partial evidence is scored, not
        refused. A candidate strong in Docker and new to Symfony is a training
        decision, and that is the bid manager's call to make."""
        matches = _run_match(
            monkeypatch,
            tender_text="Developpement Symfony et PHP, conteneurise avec Docker.",
            cv_text="Ingenieur DevOps, conteneurisation Docker et orchestration.",
        )

        assert matches[0].vetoed is False
        assert matches[0].score > 0.0
        assert matches[0].matched_technologies == ["Docker"]
        # The label a shortlist shows, rather than a filename.
        assert matches[0].to_dict()["label"] == "Jean Dupont"

    def test_a_tender_naming_no_technology_vetoes_nobody(self, monkeypatch):
        """Most Tunisian notices name no stack at all. Vetoing everyone on an
        empty requirement would return an empty shortlist for half the corpus."""
        matches = _run_match(
            monkeypatch,
            tender_text="Prestations de services divers pour l'administration.",
            cv_text="Consultant en systemes d'information.",
        )

        assert matches[0].vetoed is False


class TestTheVetoScalesWithTheDemand:
    """One match satisfied a tender naming twenty-four technologies, and one
    out of twenty-four is chance. An accountant who had once used SharePoint
    cleared the bar on a tierce-maintenance dossier and ranked third."""

    def test_a_short_stack_still_needs_only_one(self):
        from app.services.matching import _veto_floor

        assert _veto_floor(1) == 1
        assert _veto_floor(3) == 1
        assert _veto_floor(8) == 1

    def test_a_long_stack_demands_proportionate_evidence(self):
        from app.services.matching import _veto_floor

        assert _veto_floor(12) == 2
        assert _veto_floor(24) == 3

    def test_the_floor_is_capped(self):
        """A dossier listing every product in its estate must not empty the
        shortlist — an empty panel teaches a bid manager to stop opening it."""
        from app.services.matching import _veto_floor

        assert _veto_floor(80) == 3

    def test_one_match_out_of_many_is_vetoed(self, monkeypatch):
        matches = _run_match(
            monkeypatch,
            tender_text=(
                "Java Python PHP C# .NET JavaScript Go Redis Ansible GitLab REST "
                "SSO SharePoint Alfresco Drupal WordPress Docker Kubernetes"
            ),
            cv_text="Comptable. Utilise SharePoint pour le partage de documents.",
        )

        assert matches[0].vetoed is True
        assert "SharePoint" in matches[0].matched_technologies
        # The reason states the arithmetic rather than asserting a verdict.
        assert "au moins" in (matches[0].veto_reason or "")


class TestCoverageAndEvidence:
    def test_a_repeated_passage_counts_once(self, monkeypatch):
        """Otherwise a CV that says the same thing five times outranks one that
        says it once and means it."""
        matches = _run_match(
            monkeypatch,
            tender_text="Docker",
            cv_text="Docker",
            hits_per_requirement=3,
        )

        assert matches[0].coverage <= 1.0
        assert len(matches[0].evidence) == 1

    def test_every_score_carries_its_proof(self, monkeypatch):
        matches = _run_match(
            monkeypatch,
            tender_text="Docker et Kubernetes",
            cv_text="Docker, Kubernetes, GitLab",
        )

        assert matches[0].evidence
        position, score, passage = matches[0].evidence[0]
        assert isinstance(position, int)
        assert 0.0 <= score <= 1.0
        assert passage

    def test_the_serialised_form_is_auditable(self):
        payload = CandidateMatch(
            cv_id="x",
            filename="cv.pdf",
            score=0.5,
            similarity=0.6,
            coverage=0.4,
            technology_ratio=0.3,
            matched_technologies=["Docker"],
            missing_technologies=["Symfony"],
            evidence=[(0, 0.61, "passage")],
        ).to_dict()

        assert payload["matched_technologies"] == ["Docker"]
        assert payload["evidence"][0]["score"] == 0.61


class TestWeightsAreVersioned:
    def test_the_three_signals_sum_to_one(self):
        """Not required arithmetically, but a score that cannot exceed 1 is one
        a human can reason about against a threshold."""
        assert sum(MatchWeights().as_dict().values()) == pytest.approx(1.0)

    def test_a_version_travels_with_the_weights(self):
        """A past ranking must stay explainable after the weights change."""
        assert MatchWeights().version


# ---------------------------------------------------------------------------
_CV_ID = "aaaaaaaa-0000-0000-0000-000000000001"


def _run_match(monkeypatch, *, tender_text: str, cv_text: str, hits_per_requirement: int = 1):
    """Drive ``match_tender`` with the encoder and the index stubbed.

    Neither is under test here — what is, is what the platform does with their
    answers. Stubbing them also keeps these tests runnable without a 470 MB
    model on disk.
    """
    from app.services import matching as module
    from app.services.vectors import SearchHit

    class _Embedder:
        dimensions = 3

        def encode_many(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]

    class _Store:
        def search(self, collection, vector, **kwargs):
            return [
                SearchHit(
                    id=f"{_CV_ID}:{index}",
                    score=0.8,
                    payload={"owner_id": _CV_ID, "filename": "cv.pdf", "text": cv_text[:200]},
                )
                for index in range(hits_per_requirement)
            ]

    monkeypatch.setattr("app.services.embeddings.get_embedder", lambda: _Embedder())
    monkeypatch.setattr("app.services.vectors.get_vector_store", lambda: _Store())
    # Returns text *and* identity together: matching needs both per candidate,
    # and a second round trip per profile would turn one query into twenty.
    identities = {_CV_ID: ("Jean Dupont", "Consultant")}
    monkeypatch.setattr(module, "_cv_texts", lambda ids: ({_CV_ID: cv_text}, identities))

    return module.match_tender(
        tender_text=tender_text,
        requirements=[Requirement(text=tender_text, document="CCTP.pdf", position=0)],
        tenant="default",
        per_requirement=hits_per_requirement,
    )
