"""Upload validation — the gate in front of the pipeline.

A file that fails here is **not stored, not parsed, not queued, and not
counted**. It never receives a UUID. The rejection is explicit, actionable and
returned synchronously, because a user who just dragged a file in deserves to
know immediately why it bounced.

Checks run cheapest-first, and each one exists because of a real failure mode:

1. **Filename** — traversal and device names, before the value is used anywhere.
2. **Size** — enforced *while reading*, so a lying Content-Length cannot make us
   buffer a gigabyte.
3. **True MIME** — sniffed from magic bytes. The declared type and the
   extension are both attacker-controlled and are never trusted.
4. **Extension/content agreement** — a ``.pdf`` that is really a ZIP is either
   a mistake or an attack; either way it does not belong in the pipeline.
5. **Structure** — a truncated PDF or a DOCX missing its document part will
   fail deep inside a Celery worker at 3am otherwise.
6. **Active content** — macros, embedded JavaScript, auto-actions. We ingest
   documents to read them, never to execute them.

MIME sniffing is pure Python by default. ``libmagic`` is supported as an
optional escalation but is not required, because requiring a native library
just to accept a PDF makes local development on Windows unnecessarily painful.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import PurePosixPath
from typing import BinaryIO

from app.core.config import get_settings
from app.core.exceptions import (
    CorruptedFileError,
    FileTooLargeError,
    SuspiciousContentError,
    UnsupportedMediaTypeError,
    ValidationError,
)
from app.core.identity import content_fingerprint
from app.core.logging import get_logger
from app.core.metrics import document_size_bytes, validation_failures_total
from app.core.security import sanitize_filename

logger = get_logger(__name__)

__all__ = ["UploadValidator", "ValidatedUpload", "sniff_mime"]

# ---------------------------------------------------------------------------
# Magic-byte signatures
# ---------------------------------------------------------------------------
_SIGNATURES: tuple[tuple[bytes, int, str], ...] = (
    (b"%PDF-", 0, "application/pdf"),
    (b"PK\x03\x04", 0, "application/zip"),      # DOCX/XLSX/ODT are ZIP containers
    (b"PK\x05\x06", 0, "application/zip"),      # empty archive
    (b"PK\x07\x08", 0, "application/zip"),      # spanned archive
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, "application/x-ole-storage"),  # legacy .doc
    (b"{\\rtf", 0, "application/rtf"),
    (b"\x1f\x8b", 0, "application/gzip"),
    (b"Rar!\x1a\x07", 0, "application/x-rar-compressed"),
    (b"\x7fELF", 0, "application/x-executable"),
    (b"MZ", 0, "application/x-msdownload"),
    (b"#!", 0, "text/x-script"),
)

_HTML_MARKERS = (b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml")

_OOXML_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

#: Executable / active content we refuse outright, whatever the extension says.
_FORBIDDEN_MIME = frozenset(
    {
        "application/x-executable",
        "application/x-msdownload",
        "application/x-sharedlib",
        "text/x-script",
        "application/x-dosexec",
    }
)

#: PDF constructs that make a viewer *do* something rather than display it.
_PDF_ACTIVE = (
    re.compile(rb"/JavaScript\b"),
    re.compile(rb"/JS\b"),
    re.compile(rb"/OpenAction\b"),
    re.compile(rb"/AA\b"),           # additional actions
    re.compile(rb"/Launch\b"),
    re.compile(rb"/EmbeddedFile\b"),
    re.compile(rb"/RichMedia\b"),
    re.compile(rb"/XFA\b"),
)

_HTML_ACTIVE = (
    re.compile(rb"<\s*script", re.IGNORECASE),
    re.compile(rb"<\s*iframe", re.IGNORECASE),
    re.compile(rb"<\s*object", re.IGNORECASE),
    re.compile(rb"<\s*embed", re.IGNORECASE),
    re.compile(rb"javascript\s*:", re.IGNORECASE),
    re.compile(rb"\son\w+\s*=", re.IGNORECASE),   # onclick=, onerror=, ...
    re.compile(rb"<\s*meta[^>]+http-equiv\s*=\s*[\"']?refresh", re.IGNORECASE),
)


def sniff_mime(head: bytes, *, full: bytes | None = None) -> str:
    """Determine the real media type from content alone.

    ``libmagic`` is consulted first when installed, since it recognises far
    more formats; the built-in table then refines its answer for the ZIP-based
    Office formats, which libmagic reports generically.
    """
    detected: str | None = None
    try:  # pragma: no cover - depends on an optional native library
        import magic  # type: ignore[import-not-found]

        detected = magic.from_buffer(head, mime=True)
    except Exception:
        detected = None

    for signature, offset, mime in _SIGNATURES:
        if head[offset : offset + len(signature)] == signature:
            if mime == "application/zip":
                return _refine_zip(full if full is not None else head)
            return mime

    if detected:
        return detected

    sample = head[:1024].lstrip().lower()
    if any(marker in sample for marker in _HTML_MARKERS):
        return "text/html"

    try:
        head.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _refine_zip(data: bytes) -> str:
    """Distinguish a DOCX from a plain ZIP by looking inside the container."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
    except (zipfile.BadZipFile, OSError, ValueError):
        return "application/zip"
    if "word/document.xml" in names:
        return _OOXML_DOCX
    if "xl/workbook.xml" in names:
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if "ppt/presentation.xml" in names:
        return "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    if "content.xml" in names and "META-INF/manifest.xml" in names:
        return "application/vnd.oasis.opendocument.text"
    return "application/zip"


@dataclass(slots=True)
class ValidatedUpload:
    """A file that passed every check and may now enter the pipeline."""

    content: bytes
    filename: str
    original_filename: str
    content_type: str
    size_bytes: int
    extension: str
    fingerprint: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    text_preview: str | None = None


class UploadValidator:
    """Validates an uploaded document against the configured policy."""

    def __init__(self) -> None:
        settings = get_settings()
        self.max_bytes = settings.upload.max_bytes
        self.allowed_extensions = {e.lower() for e in settings.upload.allowed_extensions}
        self.allowed_mime_types = set(settings.upload.allowed_mime_types)
        self.reject_macros = settings.upload.reject_macros
        self.reject_active_pdf = settings.upload.reject_active_pdf_content
        self.reject_active_html = settings.upload.reject_active_html_content
        self.chunk_bytes = settings.upload.chunk_bytes

    # ------------------------------------------------------------------
    def _fail(self, exc: ValidationError) -> None:
        validation_failures_total.labels(reason=exc.code).inc()
        logger.warning(
            "upload.rejected",
            reason=exc.code,
            message=exc.message,
            **{k: v for k, v in exc.context.items() if k != "content"},
        )
        raise exc

    # ------------------------------------------------------------------
    def read_stream(self, stream: BinaryIO, *, declared_size: int | None = None) -> bytes:
        """Read at most ``max_bytes + 1``, then reject if the file is bigger.

        Reading one byte past the limit is what lets us detect an oversized
        file without ever holding more than the limit in memory — and without
        believing a ``Content-Length`` header the client controls.
        """
        if declared_size is not None and declared_size > self.max_bytes:
            self._fail(
                FileTooLargeError(
                    f"File is larger than the {self.max_bytes // (1024 * 1024)} MB limit.",
                    field="file",
                    context={"declared_size": declared_size, "max_bytes": self.max_bytes},
                )
            )

        buffer = BytesIO()
        total = 0
        limit = self.max_bytes + 1
        while total < limit:
            chunk = stream.read(min(self.chunk_bytes, limit - total))
            if not chunk:
                break
            buffer.write(chunk)
            total += len(chunk)

        if total > self.max_bytes:
            self._fail(
                FileTooLargeError(
                    f"File is larger than the {self.max_bytes // (1024 * 1024)} MB limit.",
                    field="file",
                    context={"max_bytes": self.max_bytes},
                )
            )
        return buffer.getvalue()

    # ------------------------------------------------------------------
    def validate(
        self,
        content: bytes,
        *,
        filename: str | None,
        declared_content_type: str | None = None,
    ) -> ValidatedUpload:
        """Run every check. Raises a specific ``ValidationError`` on the first failure."""
        original = filename or "document"
        safe_name = sanitize_filename(original)
        extension = PurePosixPath(safe_name).suffix.lower()

        if not content:
            self._fail(CorruptedFileError("File is empty.", field="file"))

        if len(content) > self.max_bytes:
            self._fail(
                FileTooLargeError(
                    f"File is larger than the {self.max_bytes // (1024 * 1024)} MB limit.",
                    field="file",
                    context={"size_bytes": len(content), "max_bytes": self.max_bytes},
                )
            )

        if extension not in self.allowed_extensions:
            self._fail(
                UnsupportedMediaTypeError(
                    f"Extension '{extension or '(none)'}' is not accepted. "
                    f"Allowed: {', '.join(sorted(self.allowed_extensions))}.",
                    field="filename",
                    context={"extension": extension},
                )
            )

        real_mime = sniff_mime(content[:8192], full=content)

        if real_mime in _FORBIDDEN_MIME:
            self._fail(
                SuspiciousContentError(
                    "File content is an executable and was refused.",
                    field="file",
                    context={"detected_mime": real_mime},
                )
            )

        normalized_mime = "text/html" if real_mime in {"text/plain", "text/html"} and extension in {
            ".html",
            ".htm",
        } else real_mime

        if normalized_mime not in self.allowed_mime_types:
            self._fail(
                UnsupportedMediaTypeError(
                    f"File content is '{normalized_mime}', which is not an accepted type.",
                    field="file",
                    context={"detected_mime": real_mime, "declared": declared_content_type},
                )
            )

        expected = self._expected_mimes(extension)
        if normalized_mime not in expected:
            self._fail(
                UnsupportedMediaTypeError(
                    f"File content ({normalized_mime}) does not match its "
                    f"'{extension}' extension.",
                    field="file",
                    context={"detected_mime": normalized_mime, "extension": extension},
                )
            )

        warnings: list[str] = []
        if declared_content_type and declared_content_type.split(";")[0].strip() != normalized_mime:
            warnings.append(
                f"Declared content type '{declared_content_type}' did not match "
                f"the detected type '{normalized_mime}'; the detected type was used."
            )

        preview = self._check_structure_and_content(content, normalized_mime, warnings)

        document_size_bytes.labels(entry_point="manual_upload").observe(len(content))
        return ValidatedUpload(
            content=content,
            filename=safe_name,
            original_filename=original,
            content_type=normalized_mime,
            size_bytes=len(content),
            extension=extension,
            fingerprint=content_fingerprint(raw=content, text=preview),
            warnings=warnings,
            text_preview=preview,
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _expected_mimes(extension: str) -> set[str]:
        return {
            ".pdf": {"application/pdf"},
            ".docx": {_OOXML_DOCX},
            ".html": {"text/html", "text/plain"},
            ".htm": {"text/html", "text/plain"},
        }.get(extension, set())

    def _check_structure_and_content(
        self, content: bytes, mime: str, warnings: list[str]
    ) -> str | None:
        if mime == "application/pdf":
            return self._check_pdf(content, warnings)
        if mime == _OOXML_DOCX:
            return self._check_docx(content, warnings)
        if mime in {"text/html", "text/plain"}:
            return self._check_html(content)
        return None

    # ------------------------------------------------------------------
    def _check_pdf(self, content: bytes, warnings: list[str]) -> None:
        if not content.startswith(b"%PDF-"):
            self._fail(CorruptedFileError("PDF header is missing.", field="file"))
        # The trailer can be followed by padding, so look in the tail rather
        # than requiring it at the very end.
        if b"%%EOF" not in content[-4096:]:
            self._fail(
                CorruptedFileError(
                    "PDF appears truncated: no end-of-file marker.", field="file"
                )
            )
        if b"/Encrypt" in content:
            warnings.append("PDF is encrypted; text extraction may fail downstream.")

        if self.reject_active_pdf:
            for pattern in _PDF_ACTIVE:
                if pattern.search(content):
                    self._fail(
                        SuspiciousContentError(
                            "PDF contains active content (scripts, auto-actions or "
                            "embedded files) and was refused.",
                            field="file",
                            context={"marker": pattern.pattern.decode("ascii", "replace")},
                        )
                    )
        return None

    def _check_docx(self, content: bytes, warnings: list[str]) -> None:
        try:
            with zipfile.ZipFile(BytesIO(content)) as archive:
                names = set(archive.namelist())
                if archive.testzip() is not None:
                    self._fail(
                        CorruptedFileError("DOCX archive is corrupted.", field="file")
                    )

                if "word/document.xml" not in names:
                    self._fail(
                        CorruptedFileError(
                            "DOCX is missing its main document part.", field="file"
                        )
                    )

                if self.reject_macros and any(
                    name.lower().endswith("vbaproject.bin") or "/vbaData" in name
                    for name in names
                ):
                    self._fail(
                        SuspiciousContentError(
                            "Document contains a macro project and was refused.",
                            field="file",
                        )
                    )

                if any(name.startswith("word/externalLink") for name in names):
                    warnings.append("Document declares external links.")

                # Zip-bomb guard: a 25 MB upload that expands to gigabytes
                # would exhaust a worker during parsing.
                declared = sum(info.file_size for info in archive.infolist())
                if declared > 40 * self.max_bytes:
                    self._fail(
                        SuspiciousContentError(
                            "Archive expands to an implausible size and was refused.",
                            field="file",
                            context={"uncompressed_bytes": declared},
                        )
                    )
        except zipfile.BadZipFile as exc:
            self._fail(
                CorruptedFileError(
                    "DOCX is not a readable archive.", field="file", cause=exc
                )
            )
        return None

    def _check_html(self, content: bytes) -> str | None:
        if self.reject_active_html:
            for pattern in _HTML_ACTIVE:
                if pattern.search(content):
                    self._fail(
                        SuspiciousContentError(
                            "HTML contains active content (script, iframe, inline event "
                            "handlers or a redirect) and was refused.",
                            field="file",
                            context={"marker": pattern.pattern.decode("ascii", "replace")},
                        )
                    )
        try:
            from app.connectors.parsing.selectors import parse_html

            soup = parse_html(content)
            text = soup.get_text(" ", strip=True)
        except Exception as exc:
            self._fail(CorruptedFileError("HTML could not be parsed.", field="file", cause=exc))
            return None
        if not text.strip():
            self._fail(CorruptedFileError("HTML contains no readable text.", field="file"))
        return text[:20_000]
