"""The boundary a CV must not cross.

The architecture reference states that CVs never leave the server (RGPD /
INPDP). Calling a hosted model contradicts that unless what is sent carries no
one's identity, so this module is the second lock behind the scope check — and
the one that has to be right, because a leak here is silent and permanent.

Two failure modes, opposite and both real. **Under-redaction** publishes an
email address. **Over-redaction** deletes a contract value or a technology and
produces a document that no longer describes anyone's competence — safe-looking
and useless. Every test below pins one side or the other.
"""

from __future__ import annotations

import pytest

from app.services.redaction import redact

CV = """RAMI OUALI
Consultant Integration
rami.ouali@inetum.com  +216 55 123 456
5-7 rue Touzet Gaillard 93400 Saint-Ouen-Sur-Seine
linkedin.com/in/rami-ouali
Ne le 12/03/1998 - CIN : 09876543

COMPETENCES
OUTILS : WSO2 API Manager, Docker, GitLab, C#, .NET 8
LANGAGES : TypeScript, Java 17, PHP 8.2, OAuth 2.0
Ouali a pilote la migration vers Kubernetes en 2023.
Budget 45000 EUR - marche de 120000 TND - disponibilite 100%
"""


@pytest.fixture()
def redacted():
    return redact(CV, known_names=["RAMI OUALI"])


class TestNothingIdentifyingSurvives:
    def test_the_email_is_gone_entirely(self, redacted):
        """Including its domain.

        Redacting the name before the email rule ran turned
        "rami.ouali@inetum.com" into "[NOM].[NOM]@inetum.com" — no longer an
        email to any pattern, so the address shipped with only its local part
        masked. Structured identifiers are removed first for this reason.
        """
        assert "@" not in redacted.text
        assert "inetum.com" not in redacted.text
        assert "[EMAIL]" in redacted.text

    def test_the_phone_is_gone(self, redacted):
        assert "55 123 456" not in redacted.text
        assert "[TEL]" in redacted.text

    def test_the_postcode_and_city_are_gone(self, redacted):
        assert "93400" not in redacted.text
        assert "Saint-Ouen" not in redacted.text

    def test_the_social_profile_is_gone(self, redacted):
        assert "linkedin" not in redacted.text.lower()

    def test_the_national_id_is_gone(self, redacted):
        assert "09876543" not in redacted.text

    def test_the_birth_date_is_gone(self, redacted):
        assert "12/03/1998" not in redacted.text

    def test_an_unaccented_birth_label_is_still_caught(self):
        """PDF extraction strips accents on some producers, so "Ne le" has to
        be caught as surely as "Né le"."""
        assert "01/02/1990" not in redact("Ne le 01/02/1990").text

    def test_the_name_is_gone_everywhere_it_appears(self, redacted):
        """Not only in the header. The surname alone, in a sentence halfway
        down the page, identifies the person just as well."""
        assert "RAMI" not in redacted.text
        assert "OUALI" not in redacted.text.upper()
        assert redacted.counts["[NOM]"] >= 2


class TestCompetenceSurvivesIntact:
    """Redaction that eats skills produces a CV nobody can be matched on."""

    @pytest.mark.parametrize(
        "term",
        ["WSO2 API Manager", "Docker", "GitLab", "C#", ".NET 8", "TypeScript",
         "Java 17", "PHP 8.2", "OAuth 2.0", "Kubernetes"],
    )
    def test_a_technology_is_never_redacted(self, redacted, term):
        assert term in redacted.text

    @pytest.mark.parametrize("amount", ["45000 EUR", "120000 TND"])
    def test_a_contract_value_is_never_redacted(self, redacted, amount):
        """Requiring only a capital after five digits matched "45000 EUR" as a
        postal address and deleted a budget. The place name must now be
        Capitalised-then-lowercase, which a currency code is not.
        """
        assert amount in redacted.text

    def test_a_year_is_not_mistaken_for_a_phone_number(self, redacted):
        assert "2023" in redacted.text

    def test_a_percentage_survives(self, redacted):
        assert "100%" in redacted.text


class TestBoundariesBetweenPatterns:
    def test_a_phone_does_not_swallow_the_following_line(self):
        """With whitespace in the separator class, "+216 55 123 456" followed
        by "5-7 rue" came back as "[TEL] rue" — the street number eaten by a
        pattern that crossed a newline."""
        result = redact("+216 55 123 456\n5-7 rue Touzet Gaillard")

        assert "5-7 rue" in result.text

    def test_redaction_replaces_rather_than_deletes(self):
        """The model still has to see a sentence. "contacter [NOM] à [EMAIL]"
        keeps its shape; deleting the spans leaves "contacter à", which is
        worse input for a worse answer."""
        result = redact("Contacter Jean Dupont a jean@x.fr", known_names=["Jean Dupont"])

        assert "[NOM]" in result.text
        assert "[EMAIL]" in result.text
        assert "Contacter" in result.text

    def test_an_empty_document_is_not_an_error(self):
        assert redact("").text == ""
        assert redact("", known_names=["X"]).total == 0

    def test_a_very_short_name_is_ignored(self):
        """A two-letter "name" would match inside ordinary words and gut the
        document. Initials shorter than three characters are left alone."""
        result = redact("Le SI de la direction", known_names=["SI"])

        assert "SI" in result.text


class TestTheReportSaysWhatWasTakenNotWhat:
    def test_counts_are_recorded(self, redacted):
        assert redacted.total >= 6
        assert set(redacted.counts) <= {
            "[NOM]", "[EMAIL]", "[TEL]", "[ADRESSE]", "[PROFIL]",
            "[NAISSANCE]", "[IDENTIFIANT]",
        }

    def test_the_report_never_carries_the_values(self, redacted):
        """A log recording what was redacted has moved the personal data
        somewhere else rather than removed it."""
        blob = " ".join(redacted.counts)

        assert "ouali" not in blob.lower()
        assert "@" not in blob
