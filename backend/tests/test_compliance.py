"""The compliance matrix, and the three ways it could lie.

This document's whole value is its empty cells. A matrix that shows green
everywhere tells a bid manager nothing they had not already assumed, and a
matrix that shows green *wrongly* is worse than none — it is a false assurance
on a contractual submission.

So the tests are not about arithmetic. They are about the three temptations:
claiming coverage the evidence does not support, quoting a passage that proves
something else, and hiding what the platform cannot judge inside a percentage.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.compliance import build_matrix


def _entry(label: str, technologies: list[str], evidence: list[str] | None = None):
    return SimpleNamespace(
        label=label,
        matched_technologies=technologies,
        evidence=[
            {"requirement": index, "score": 0.9 - index / 10, "passage": text}
            for index, text in enumerate(evidence or [])
        ],
    )


def _shortlist(technologies: list[str], obligations: list[str] | None = None):
    return SimpleNamespace(
        required_technologies=technologies,
        requirements=[{"position": 1, "document": "CCTP", "text": "Java et Spring"}],
        structured_requirements={"exigences": obligations or []},
        tender_title="TMA du SI ARIA",
    )


class TestCoverageIsClaimedOnlyWhereItIsEvidenced:
    def test_a_technology_nobody_holds_is_marked_uncovered(self):
        """The row that earns the document: a gap found before the bid is
        written rather than during its defence."""
        matrix = build_matrix(
            _shortlist(["Java", "Kubernetes"]),
            [_entry("Marie Dupont", ["Java"], ["Développement Java 17"])],
        )
        rows = {row.label: row for row in matrix.rows}

        assert rows["Kubernetes"].statut == "non_couverte"
        assert rows["Kubernetes"].profils == []
        assert "Aucun profil retenu" in rows["Kubernetes"].note

    def test_a_held_technology_names_who_holds_it(self):
        matrix = build_matrix(
            _shortlist(["Java"]),
            [
                _entry("Marie Dupont", ["Java"], ["Développement Java 17"]),
                _entry("Ahmed Ben Ali", ["Java"], ["Migration Java du socle"]),
            ],
        )

        assert matrix.rows[0].statut == "couverte"
        assert matrix.rows[0].profils == ["Marie Dupont", "Ahmed Ben Ali"]

    def test_the_match_ignores_case(self):
        matrix = build_matrix(
            _shortlist(["JAVA"]), [_entry("Marie", ["java"], ["Projet java"])]
        )

        assert matrix.rows[0].statut == "couverte"


class TestAQuotationMustProveItsOwnRow:
    """The first version returned each profile's highest-scoring passage
    whatever the row was about, so one quotation appeared beside TypeScript,
    Docker, Kubernetes and GitLab — under a column headed "éléments probants",
    proving none of them."""

    def test_only_passages_naming_the_technology_are_quoted(self):
        matrix = build_matrix(
            _shortlist(["Docker"]),
            [
                _entry(
                    "Marie Dupont",
                    ["Docker"],
                    [
                        "Animation de conférences sur les systèmes experts",
                        "Conteneurisation des services avec Docker et Compose",
                    ],
                )
            ],
        )

        assert len(matrix.rows[0].evidence) == 1
        assert "Docker" in matrix.rows[0].evidence[0]
        assert "conférences" not in matrix.rows[0].evidence[0]

    def test_a_covered_row_with_no_quotation_explains_itself(self):
        """Empty is honest — the skill is in the profile's evidenced list and
        no kept passage happens to name it. An unexplained blank cell reads as
        a defect, and an unrelated quotation reads as proof."""
        matrix = build_matrix(
            _shortlist(["Docker"]),
            [_entry("Marie Dupont", ["Docker"], ["Animation de conférences"])],
        )

        assert matrix.rows[0].statut == "couverte"
        assert matrix.rows[0].evidence == []
        assert "hors des passages retenus" in matrix.rows[0].note

    def test_the_quotation_carries_the_profile_it_came_from(self):
        matrix = build_matrix(
            _shortlist(["Java"]), [_entry("Marie Dupont", ["Java"], ["Socle Java 17"])]
        )

        assert matrix.rows[0].evidence[0].startswith("Marie Dupont —")


class TestWhatCannotBeJudgedIsNeverScored:
    """Measured on a real tender, the obligations read "Décrire la méthode
    envisagée", "Estimer le coût global", "Être capable d'intervenir sur site
    au siège de Limoges". None is answerable from a CV."""

    def test_an_obligation_is_never_marked_covered(self):
        matrix = build_matrix(
            _shortlist(["Java"], ["Estimer le coût global de la prestation"]),
            [_entry("Marie", ["Java"], ["Java 17"])],
        )
        obligation = next(r for r in matrix.rows if r.section == "Exigences du dossier")

        assert obligation.statut == "a_traiter"

    def test_an_obligation_naming_a_held_technology_offers_a_lead_not_a_verdict(self):
        matrix = build_matrix(
            _shortlist(["Java"], ["Assurer la maintenance des applications Java"]),
            [_entry("Marie Dupont", ["Java"], ["Java 17"])],
        )
        obligation = next(r for r in matrix.rows if r.section == "Exigences du dossier")

        assert obligation.statut == "a_traiter"
        assert "Piste" in obligation.note
        assert "à confirmer par un humain" in obligation.note
        assert obligation.profils == ["Marie Dupont"]

    def test_an_obligation_with_no_lead_says_so(self):
        matrix = build_matrix(
            _shortlist(["Java"], ["Être capable d'intervenir sur site à Limoges"]),
            [_entry("Marie", ["Java"], ["Java 17"])],
        )
        obligation = next(r for r in matrix.rows if r.section == "Exigences du dossier")

        assert "À rédiger" in obligation.note

    def test_the_ratio_excludes_what_cannot_be_judged(self):
        """A denominator that swallowed the unassessable rows would turn an
        unknown into a success — on a document someone signs."""
        matrix = build_matrix(
            _shortlist(
                ["Java", "Kubernetes"],
                ["Décrire la méthode", "Estimer le coût", "Intervenir sur site"],
            ),
            [_entry("Marie", ["Java"], ["Java 17"])],
        )

        assert matrix.couvertes == 1
        assert matrix.non_couvertes == 1
        assert matrix.a_traiter == 3
        # 1 of 2 assessable, not 1 of 5.
        assert matrix.taux == 50

    def test_the_unassessable_count_is_reported_separately(self):
        payload = build_matrix(
            _shortlist(["Java"], ["Décrire la méthode"]), [_entry("Marie", ["Java"], [])]
        ).to_dict()

        assert payload["a_traiter"] == 1
        assert payload["taux_couverture"] == 100


class TestTheEdges:
    def test_a_tender_naming_no_technology_yields_no_technology_rows(self):
        matrix = build_matrix(_shortlist([]), [_entry("Marie", ["Java"], [])])

        assert [r for r in matrix.rows if r.section == "Technologies exigées"] == []
        assert matrix.taux == 0

    def test_a_shortlist_with_no_retained_profile_marks_everything_uncovered(self):
        matrix = build_matrix(_shortlist(["Java", "Docker"]), [])

        assert matrix.couvertes == 0
        assert matrix.non_couvertes == 2
        assert matrix.taux == 0

    def test_the_rows_serialise_flat_for_the_template(self):
        """docxtpl loops over records and prints fields; a list inside a cell
        would render as a Python repr in a submitted document."""
        row = build_matrix(
            _shortlist(["Java"]), [_entry("Marie", ["Java"], ["Java 17"])]
        ).to_dict()["lignes"][0]

        assert all(isinstance(value, str) for value in row.values())
