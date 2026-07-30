"""Upload validation — the gate in front of the pipeline."""

from __future__ import annotations

import io

import pytest

from app.core.exceptions import (
    CorruptedFileError,
    FileTooLargeError,
    SuspiciousContentError,
    UnsupportedMediaTypeError,
)
from app.services.validation import UploadValidator, sniff_mime


@pytest.fixture()
def validator() -> UploadValidator:
    return UploadValidator()


class TestMimeSniffing:
    def test_pdf_by_magic_bytes(self, minimal_pdf):
        assert sniff_mime(minimal_pdf[:8192], full=minimal_pdf) == "application/pdf"

    def test_docx_is_distinguished_from_a_plain_zip(self, minimal_docx):
        assert sniff_mime(minimal_docx[:8192], full=minimal_docx) == (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def test_html_by_content(self):
        html = b"<!DOCTYPE html><html><body>Avis</body></html>"
        assert sniff_mime(html, full=html) == "text/html"

    def test_executables_are_identified(self):
        assert sniff_mime(b"MZ\x90\x00" + b"\x00" * 100) == "application/x-msdownload"


class TestAcceptedUploads:
    def test_valid_pdf(self, validator, minimal_pdf):
        result = validator.validate(minimal_pdf, filename="cahier des charges.pdf")
        assert result.content_type == "application/pdf"
        assert result.filename == "cahier_des_charges.pdf"
        assert result.fingerprint["raw_sha256"]

    def test_valid_docx(self, validator, minimal_docx):
        result = validator.validate(minimal_docx, filename="dossier.docx")
        assert result.extension == ".docx"

    def test_valid_html_yields_a_text_preview(self, validator):
        html = b"<html><body><h1>Avis d'appel d'offres</h1><p>Objet du marche</p></body></html>"
        result = validator.validate(html, filename="avis.html")
        assert "Avis d'appel d'offres" in result.text_preview

    def test_mismatched_declared_type_is_a_warning_not_a_rejection(
        self, validator, minimal_pdf
    ):
        # The client lied about the type; the sniffed type is authoritative and
        # the discrepancy is surfaced rather than silently ignored.
        result = validator.validate(
            minimal_pdf, filename="doc.pdf", declared_content_type="text/plain"
        )
        assert result.content_type == "application/pdf"
        assert result.warnings


class TestRejectedUploads:
    def test_empty_file(self, validator):
        with pytest.raises(CorruptedFileError):
            validator.validate(b"", filename="empty.pdf")

    def test_disallowed_extension(self, validator, minimal_pdf):
        with pytest.raises(UnsupportedMediaTypeError):
            validator.validate(minimal_pdf, filename="payload.exe")

    def test_extension_that_lies_about_its_content(self, validator, minimal_docx):
        # A DOCX renamed to .pdf: either a mistake or an attack, never valid.
        with pytest.raises(UnsupportedMediaTypeError) as excinfo:
            validator.validate(minimal_docx, filename="notice.pdf")
        assert "does not match" in excinfo.value.message

    def test_executable_content(self, validator):
        with pytest.raises(SuspiciousContentError):
            validator.validate(b"MZ\x90\x00" + b"\x00" * 200, filename="doc.pdf")

    def test_truncated_pdf(self, validator):
        with pytest.raises(CorruptedFileError) as excinfo:
            validator.validate(b"%PDF-1.4\nsome content but no trailer", filename="x.pdf")
        assert "truncated" in excinfo.value.message.lower()

    def test_pdf_with_embedded_javascript(self, validator, minimal_pdf):
        hostile = minimal_pdf.replace(b"trailer", b"/JavaScript (app.alert\\(1\\))\ntrailer")
        with pytest.raises(SuspiciousContentError) as excinfo:
            validator.validate(hostile, filename="x.pdf")
        assert "active content" in excinfo.value.message

    def test_pdf_with_an_auto_action(self, validator, minimal_pdf):
        hostile = minimal_pdf.replace(b"trailer", b"/OpenAction 5 0 R\ntrailer")
        with pytest.raises(SuspiciousContentError):
            validator.validate(hostile, filename="x.pdf")

    def test_docx_with_macros(self, validator, macro_docx):
        with pytest.raises(SuspiciousContentError) as excinfo:
            validator.validate(macro_docx, filename="x.docx")
        assert "macro" in excinfo.value.message.lower()

    def test_docx_missing_its_document_part(self, validator):
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("random.txt", "not a docx")
        with pytest.raises(UnsupportedMediaTypeError):
            validator.validate(buffer.getvalue(), filename="x.docx")

    @pytest.mark.parametrize(
        "hostile_html",
        [
            b"<html><script>alert(1)</script></html>",
            b'<html><iframe src="http://evil.tn"></iframe></html>',
            b'<html><img src=x onerror="fetch(\'/x\')"></html>',
            b'<html><meta http-equiv="refresh" content="0;url=http://evil.tn"></html>',
        ],
    )
    def test_html_with_active_content(self, validator, hostile_html):
        with pytest.raises(SuspiciousContentError):
            validator.validate(hostile_html, filename="x.html")

    def test_oversized_file(self, validator, minimal_pdf):
        validator.max_bytes = 100
        with pytest.raises(FileTooLargeError):
            validator.validate(minimal_pdf + b"x" * 500, filename="x.pdf")


class TestStreamingSizeEnforcement:
    def test_size_is_enforced_while_reading(self, validator):
        validator.max_bytes = 1024
        validator.chunk_bytes = 128
        stream = io.BytesIO(b"x" * 5000)
        with pytest.raises(FileTooLargeError):
            validator.read_stream(stream)

    def test_a_lying_content_length_is_rejected_up_front(self, validator):
        validator.max_bytes = 1024
        with pytest.raises(FileTooLargeError):
            validator.read_stream(io.BytesIO(b"x"), declared_size=999_999_999)

    def test_a_file_at_the_limit_is_accepted(self, validator):
        validator.max_bytes = 1024
        payload = b"x" * 1024
        assert validator.read_stream(io.BytesIO(payload)) == payload


class TestRejectionIsTotal:
    def test_a_rejected_file_never_produces_a_validated_upload(self, validator):
        """Nothing downstream can see a rejected file: the only way to obtain a
        ``ValidatedUpload`` is for every check to pass."""
        with pytest.raises(SuspiciousContentError):
            validator.validate(b"<html><script>x</script></html>", filename="x.html")
