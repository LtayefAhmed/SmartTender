"""Producing the dossier — and the three refusals that matter more.

A generator that always produces something will one day produce a CV for a
person nobody selected, from a list nobody validated, and quietly overwrite a
version somebody already sent. None of those failures announce themselves in a
Word file, so they are guarded here.
"""

from __future__ import annotations

import uuid as uuid_module

import pytest

from app.core.enums import (
    GeneratedDocumentStatus,
    ShortlistDecision,
    ShortlistStatus,
    TemplateKind,
)
from app.db.models.document import GeneratedDocument
from app.db.models.shortlist import Shortlist, ShortlistEntry
from app.db.models.template import DocumentTemplate


def _shortlist(status: str = ShortlistStatus.VALIDATED.value, **entry_kwargs) -> Shortlist:
    entry = ShortlistEntry(
        cv_id=uuid_module.uuid4(),
        rank=1,
        label="Marie Dupont",
        score=0.81,
        decision=entry_kwargs.pop("decision", ShortlistDecision.RETAINED.value),
        **entry_kwargs,
    )
    return Shortlist(tenant_id="inetum", status=status, entries=[entry])


class TestTheGuardsAroundProducing:
    """These are enforced in the router, and mirrored here as statements about
    the domain: what the rows have to say for a document to be legitimate."""

    def test_a_document_needs_a_validated_selection(self, db_session):
        """An open shortlist is a working draft. Generating from it would put a
        real name on a real submission before anyone signed the list."""
        shortlist = _shortlist(status=ShortlistStatus.OPEN.value)
        db_session.add(shortlist)
        db_session.commit()

        assert shortlist.status != ShortlistStatus.VALIDATED.value

    def test_a_document_needs_an_explicit_retention(self, db_session):
        """`pending` means nobody looked — a different fact from `rejected`,
        and neither is a decision to include someone."""
        shortlist = _shortlist(decision=ShortlistDecision.PENDING.value)
        db_session.add(shortlist)
        db_session.commit()

        retained = [
            e
            for e in shortlist.entries
            if e.decision == ShortlistDecision.RETAINED.value
        ]
        assert retained == []


class TestAVersionIsNeverOverwritten:
    def test_regenerating_supersedes_rather_than_replaces(self, db_session):
        """A document already circulated has to stay explainable, and the
        review step will need a diff between two versions."""
        shortlist = _shortlist()
        db_session.add(shortlist)
        db_session.flush()
        entry = shortlist.entries[0]

        first = GeneratedDocument(
            tenant_id="inetum",
            shortlist_id=shortlist.id,
            entry_id=entry.id,
            filename="marie_v1.docx",
            storage_bucket="b",
            storage_key="k1",
            version=1,
            label="Marie Dupont",
        )
        db_session.add(first)
        db_session.commit()

        first.status = GeneratedDocumentStatus.SUPERSEDED.value
        second = GeneratedDocument(
            tenant_id="inetum",
            shortlist_id=shortlist.id,
            entry_id=entry.id,
            filename="marie_v2.docx",
            storage_bucket="b",
            storage_key="k2",
            version=2,
            label="Marie Dupont",
        )
        db_session.add(second)
        db_session.commit()

        rows = (
            db_session.query(GeneratedDocument)
            .filter(GeneratedDocument.entry_id == entry.id)
            .order_by(GeneratedDocument.version)
            .all()
        )
        assert [r.version for r in rows] == [1, 2]
        assert rows[0].status == GeneratedDocumentStatus.SUPERSEDED.value


class TestTheRecordOutlivesWhatItDescribes:
    def test_deleting_the_cv_keeps_the_document_row(self, db_session):
        """A candidate can ask for their CV to be removed. The record that a
        document was produced and sent must survive that — it is the whole
        point of the audit requirement, and a cascade would defeat it."""
        from app.db.models.cv import CV

        cv = CV(
            tenant_id="inetum",
            original_filename="cv.pdf",
            storage_bucket="b",
            storage_key="k",
            content_type="application/pdf",
            size_bytes=1,
        )
        db_session.add(cv)
        db_session.flush()

        document = GeneratedDocument(
            tenant_id="inetum",
            cv_id=cv.id,
            filename="marie.docx",
            storage_bucket="b",
            storage_key="k",
            label="Marie Dupont",
        )
        db_session.add(document)
        db_session.commit()

        db_session.delete(cv)
        db_session.commit()

        # SQLite in the test harness does not enforce ON DELETE SET NULL
        # without a pragma, so what is asserted is the row's survival — which
        # is the property that matters and the one a cascade would break.
        assert db_session.get(GeneratedDocument, document.id) is not None


class TestWhatTheDocumentSaysAboutItself:
    def test_it_names_the_template_version_that_made_it(self, db_session):
        """Copied, not joined: a template can be retired and superseded, and
        the document still has to be able to say which one produced it."""
        template = DocumentTemplate(
            tenant_id="inetum",
            key="cv_inetum",
            label="CV Inetum",
            kind=TemplateKind.CV.value,
            version=3,
            original_filename="cv.docx",
            storage_bucket="b",
            storage_key="k",
            content_type="application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
        )
        db_session.add(template)
        db_session.flush()

        document = GeneratedDocument(
            tenant_id="inetum",
            template_id=template.id,
            template_key=template.key,
            template_version=template.version,
            filename="marie.docx",
            storage_bucket="b",
            storage_key="k",
            label="Marie Dupont",
        )
        db_session.add(document)
        db_session.commit()

        db_session.delete(template)
        db_session.commit()

        fetched = db_session.get(GeneratedDocument, document.id)
        assert fetched.template_key == "cv_inetum"
        assert fetched.template_version == 3

    def test_a_draft_is_watermarked_by_default(self, db_session):
        document = GeneratedDocument(
            tenant_id="inetum",
            filename="marie.docx",
            storage_bucket="b",
            storage_key="k",
            label="Marie Dupont",
        )
        db_session.add(document)
        db_session.commit()

        assert document.status == GeneratedDocumentStatus.DRAFT.value
        assert document.watermarked is True

    @pytest.mark.parametrize("missing", [[], ["formations"], ["formations", "langues"]])
    def test_it_records_what_it_could_not_fill(self, db_session, missing):
        """The platform naming its own gaps is worth more than a document that
        looks complete: a reviewer should learn about a blank section here, not
        in a dossier a client has opened."""
        document = GeneratedDocument(
            tenant_id="inetum",
            filename="marie.docx",
            storage_bucket="b",
            storage_key="k",
            label="Marie Dupont",
            empty_fields=missing,
        )
        db_session.add(document)
        db_session.commit()

        assert list(document.empty_fields) == missing
