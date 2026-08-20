"""Cutting a dossier into passages that still mean something.

A vector is an average of everything its text says. Our richest tender holds
441 446 characters spanning a CCTP, a privacy notice and a signature block, and
averaging those produces a point near nothing in particular. These tests pin
the three properties that keep a passage meaningful: it knows which document it
came from, it does not begin or end mid-sentence, and no span of the source
falls between two passages.
"""

from __future__ import annotations

from app.services.chunking import Chunk, chunk_text, split_by_document
from app.services.extraction import DOCUMENT_MARKER, document_priority

MARKER = DOCUMENT_MARKER.strip()


def _dossier() -> str:
    return (
        f"{MARKER} CCTP_2026.pdf\n"
        "Le titulaire assure la tierce maintenance applicative du systeme.\n"
        "Les technologies concernees sont Java 17, Angular 15 et PostgreSQL.\n\n"
        "La disponibilite exigee est de 99,5 pour cent sur les heures ouvrees.\n\n"
        f"{MARKER} DC1.doc\n"
        "Le present formulaire est a completer par le candidat avant depot.\n"
    )


class TestProvenanceSurvivesChunking:
    """A passage nobody can attribute cannot be weighted, and weighting is the
    whole point: there is far more boilerplate than specification."""

    def test_each_passage_names_its_document(self):
        documents = {chunk.document for chunk in chunk_text(_dossier(), chunk_chars=200)}

        assert documents == {"CCTP_2026.pdf", "DC1.doc"}

    def test_a_passage_never_spans_two_documents(self):
        """A chunk straddling the CCTP and a form belongs to neither."""
        for chunk in chunk_text(_dossier(), chunk_chars=4_000):
            assert "DOCUMENT:" not in chunk.text

    def test_the_specification_outranks_the_form(self):
        chunks = chunk_text(_dossier(), chunk_chars=200)
        cctp = [c for c in chunks if c.document == "CCTP_2026.pdf"]
        form = [c for c in chunks if c.document == "DC1.doc"]

        assert all(c.priority == 0 for c in cctp)
        assert all(c.priority == 1 for c in form)

    def test_text_without_markers_is_still_chunked(self):
        """A publication body has no document structure and must not be lost."""
        chunks = chunk_text("Objet du marche. " * 200)

        assert chunks
        assert all(chunk.document is None for chunk in chunks)

    def test_a_preamble_before_the_first_marker_is_kept(self):
        sections = split_by_document(f"Texte de publication.\n{MARKER} CCTP.pdf\nExigences.")

        assert sections[0] == (None, "Texte de publication.")
        assert sections[1] == ("CCTP.pdf", "Exigences.")


class TestPassagesAreCutWhereMeaningAllows:
    def test_a_paragraph_break_is_preferred(self):
        body = "Premiere partie du texte, assez longue pour compter.\n\n" + "Seconde partie. " * 20
        chunks = chunk_text(body, chunk_chars=120, min_chars=10)

        assert chunks[0].text.endswith("compter.")

    def test_a_sentence_end_is_used_when_no_paragraph_break_exists(self):
        body = "Phrase une, suffisamment longue pour remplir. Phrase deux qui suit derriere."
        chunks = chunk_text(body, chunk_chars=60, min_chars=10)

        assert chunks[0].text.endswith(".")

    def test_a_word_is_never_split(self):
        body = " ".join(f"terme{index}" for index in range(300))
        for chunk in chunk_text(body, chunk_chars=200):
            assert not chunk.text.startswith("erme")
            assert chunk.text.split()[-1].startswith("terme")

    def test_headings_and_page_numbers_are_dropped(self):
        """A five-character passage embeds as a vector close to everything and
        useful for nothing."""
        chunks = chunk_text(f"{MARKER} a.pdf\n{'Contenu reel. ' * 200}\n\n12\n")

        assert all(chunk.char_count >= 80 for chunk in chunks)


class TestNothingFallsBetweenTwoPassages:
    def test_overlap_carries_a_sentence_across_a_boundary(self):
        body = "".join(f"Exigence numero {i} du cahier des charges technique. " for i in range(40))
        chunks = chunk_text(body, chunk_chars=300, overlap_chars=100)

        assert len(chunks) > 1
        # The tail of one passage reappears at the head of the next, so a
        # requirement sitting on the seam is readable whole at least once.
        assert any(chunks[0].text[-40:] in chunks[1].text for _ in [0]) or chunks[1].text[:40] in (
            chunks[0].text
        )

    def test_every_character_of_the_source_appears_somewhere(self):
        body = "".join(f"Phrase numero {i} avec du contenu. " for i in range(60))
        joined = " ".join(chunk.text for chunk in chunk_text(body, chunk_chars=250))

        for index in (0, 17, 42, 59):
            assert f"Phrase numero {index}" in joined

    def test_chunking_terminates_on_pathological_input(self):
        """A cut point at or before the overlap would loop forever without the
        forward-progress guard."""
        chunks = chunk_text("a" * 5_000, chunk_chars=100, overlap_chars=100)

        assert 0 < len(chunks) < 200

    def test_empty_input_yields_nothing(self):
        assert chunk_text("") == []
        assert chunk_text("   \n  ") == []


class TestFilenamePriorityIsAPriorNotAVerdict:
    """Measured against filenames from real dossiers in the corpus."""

    def test_a_hyphenated_reglement_de_consultation_is_substantive(self):
        """`_rc` / `rc_` missed "AWS-MPI-1857755-RC.pdf" because the separator
        was a hyphen — a règlement de consultation ranked as filler."""
        assert document_priority("AWS-MPI-1857755-RC.pdf") == 0

    def test_rc_inside_an_ordinary_word_does_not_count(self):
        """The reason the separator workaround existed in the first place."""
        assert document_priority("recherche_partenaire.pdf") == 1

    def test_technical_annexes_are_substantive_whatever_they_are_called(self):
        """These three name the required technologies — .NET, Docker,
        Kubernetes — and match none of the procurement acronyms."""
        assert document_priority("Okantis-Ard-Architecture.pdf") == 0
        assert document_priority("OKANTIS-ArDe-Guideslines-Securite.v1.2.pdf") == 0
        # Misspelled in the live dossier; `develop` covers it.
        assert document_priority("Okantis-Ard-Developement-1.1.0.pdf") == 0

    def test_administrative_forms_stay_filler(self):
        assert document_priority("ATTRI1.pdf") == 1
        assert document_priority("DC1.doc") == 1
        assert document_priority(None) == 1


class TestChunkShape:
    def test_indices_are_contiguous_and_ordered(self):
        chunks = chunk_text(_dossier(), chunk_chars=150)

        assert [c.index for c in chunks] == list(range(len(chunks)))

    def test_char_count_matches_the_text(self):
        chunk = Chunk(text="abc", document="x.pdf", index=0)

        assert chunk.char_count == 3
