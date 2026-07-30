"""Document text extraction, with OCR fallback.

This is what turns a stored PDF into something the scoring engine can actually
read. Without it, ``field_of_work`` and ``keywords`` — 55% of the total weight
— evaluate a tender on its title alone, and the CV-matching module downstream
has no requirements to match against.

The strategy is cheapest-first, the same principle as duplicate detection:

    1. **Digital text layer** (pypdf / python-docx / BeautifulSoup).
       Pure Python, milliseconds, no system dependency. Handles the large
       majority of tender documents, which are produced digitally.

    2. **OCR** (pypdfium2 → OpenCV → Tesseract), *per page*, only where the
       text layer came back essentially empty.

Doing the fallback per page rather than per document matters: tender files are
routinely hybrid — a digital cover page followed by a scanned, stamped annex.
Choosing one strategy for the whole file either wastes a second per page on
pages that did not need it, or silently loses the pages that did.

Every failure degrades rather than raises. A document that cannot be read
leaves the tender with less text, never without a tender.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.metrics import observe_stage

logger = get_logger(__name__)

__all__ = ["DocumentExtractor", "ExtractedText", "get_extractor"]

_WHITESPACE = re.compile(r"[ \t ]+")
_BLANK_LINES = re.compile(r"\n{3,}")
#: Ligatures and dashes that PDF producers emit and that break keyword matching.
_PDF_ARTEFACTS = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "­": "",
}


@dataclass(slots=True)
class ExtractedText:
    """What one document yielded."""

    text: str = ""
    method: str = "none"          # digital | ocr | mixed | none
    pages_total: int = 0
    pages_ocr: int = 0
    truncated: bool = False
    error: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.text.strip())

    @property
    def char_count(self) -> int:
        return len(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "chars": self.char_count,
            "pages_total": self.pages_total,
            "pages_ocr": self.pages_ocr,
            "truncated": self.truncated,
            "error": self.error,
            "warnings": self.warnings,
        }


def clean_extracted_text(raw: str, *, max_chars: int | None = None) -> tuple[str, bool]:
    """Normalise extracted text for storage and scoring."""
    if not raw:
        return "", False

    text = raw
    for bad, good in _PDF_ARTEFACTS.items():
        text = text.replace(bad, good)

    # PDF extraction hyphenates across line breaks; rejoining is what makes
    # "développe-\nment" match the keyword "développement".
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WHITESPACE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = _BLANK_LINES.sub("\n\n", text).strip()

    truncated = False
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0]
        truncated = True
    return text, truncated


class DocumentExtractor:
    """Extracts readable text from PDF, DOCX and HTML documents."""

    def __init__(self) -> None:
        settings = get_settings().extraction
        self.enabled = settings.enabled
        self.ocr_enabled = settings.ocr_enabled
        self.tesseract_cmd = settings.tesseract_cmd
        self.ocr_languages = settings.ocr_languages
        self.min_chars_before_ocr = settings.min_chars_before_ocr
        self.max_ocr_pages = settings.max_ocr_pages
        self.ocr_dpi = settings.ocr_dpi
        self.max_pdf_pages = settings.max_pdf_pages
        self.max_chars = settings.max_chars_per_document
        self.max_document_bytes = settings.max_document_bytes
        self._ocr_available: bool | None = None

    # ------------------------------------------------------------------
    def extract(self, content: bytes, *, content_type: str | None = None,
                filename: str | None = None) -> ExtractedText:
        """Extract text from a document, dispatching on its real type."""
        if not self.enabled:
            return ExtractedText(method="none", error="Extraction is disabled.")
        if not content:
            return ExtractedText(method="none", error="Document is empty.")
        if len(content) > self.max_document_bytes:
            return ExtractedText(
                method="none",
                error=f"Document exceeds the {self.max_document_bytes} byte extraction limit.",
            )

        kind = self._detect(content, content_type, filename)
        with observe_stage("extract"):
            if kind == "pdf":
                return self._extract_pdf(content)
            if kind == "docx":
                return self._extract_docx(content)
            if kind == "html":
                return self._extract_html(content)
            if kind == "text":
                text, truncated = clean_extracted_text(
                    content.decode("utf-8", errors="replace"), max_chars=self.max_chars
                )
                return ExtractedText(text=text, method="digital", truncated=truncated)

        return ExtractedText(method="none", error=f"Unsupported document type '{kind}'.")

    @staticmethod
    def _detect(content: bytes, content_type: str | None, filename: str | None) -> str:
        """Determine the real type. Content wins over the declared type."""
        from app.services.validation import sniff_mime

        mime = sniff_mime(content[:8192], full=content)
        if mime == "application/pdf":
            return "pdf"
        if "wordprocessingml" in mime:
            return "docx"
        if mime in {"text/html"}:
            return "html"
        if mime == "text/plain":
            # A declared .html with a text/plain sniff is still HTML.
            suffix = (filename or "").lower()
            return "html" if suffix.endswith((".html", ".htm")) else "text"
        return mime

    # ------------------------------------------------------------------
    def _extract_pdf(self, content: bytes) -> ExtractedText:
        """Digital text first, per-page OCR only where it came back empty."""
        try:
            from pypdf import PdfReader
        except ImportError:  # pragma: no cover - declared dependency
            return ExtractedText(method="none", error="pypdf is not installed.")

        warnings: list[str] = []
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                # Many tender PDFs carry an owner password with an empty user
                # password; that decrypts silently and is worth attempting.
                try:
                    reader.decrypt("")
                except Exception:
                    return ExtractedText(
                        method="none", error="PDF is encrypted and could not be opened."
                    )
            pages = reader.pages[: self.max_pdf_pages]
            total_pages = len(reader.pages)
        except Exception as exc:
            return ExtractedText(method="none", error=f"Unreadable PDF: {exc}")

        if total_pages > self.max_pdf_pages:
            warnings.append(
                f"Only the first {self.max_pdf_pages} of {total_pages} pages were read."
            )

        page_texts: list[str] = []
        needs_ocr: list[int] = []
        for index, page in enumerate(pages):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            page_texts.append(text)
            if len(text.strip()) < self.min_chars_before_ocr:
                needs_ocr.append(index)

        pages_ocr = 0
        if needs_ocr and self.ocr_enabled and self._ensure_ocr():
            budget = needs_ocr[: self.max_ocr_pages]
            if len(needs_ocr) > self.max_ocr_pages:
                warnings.append(
                    f"{len(needs_ocr)} pages needed OCR; only {self.max_ocr_pages} were processed."
                )
            recovered = self._ocr_pages(content, budget)
            for index, text in recovered.items():
                if text.strip():
                    page_texts[index] = text
                    pages_ocr += 1
        elif needs_ocr and self.ocr_enabled:
            warnings.append(
                f"{len(needs_ocr)} page(s) appear scanned but OCR is unavailable."
            )

        joined = "\n\n".join(t for t in page_texts if t and t.strip())
        text, truncated = clean_extracted_text(joined, max_chars=self.max_chars)

        if pages_ocr and pages_ocr < len(page_texts):
            method = "mixed"
        elif pages_ocr:
            method = "ocr"
        elif text:
            method = "digital"
        else:
            method = "none"

        return ExtractedText(
            text=text,
            method=method,
            pages_total=len(pages),
            pages_ocr=pages_ocr,
            truncated=truncated,
            warnings=warnings,
            error=None if text else "No readable text found in the PDF.",
        )

    # ------------------------------------------------------------------
    def _ensure_ocr(self) -> bool:
        """Check once whether Tesseract is genuinely usable."""
        if self._ocr_available is not None:
            return self._ocr_available
        try:
            import pypdfium2  # noqa: F401
            import pytesseract

            if self.tesseract_cmd:
                pytesseract.pytesseract.tesseract_cmd = self.tesseract_cmd
            # Invoke it: an installed wrapper with a missing binary is the
            # common failure, and it only surfaces on a real call.
            pytesseract.get_tesseract_version()
            self._ocr_available = True
            logger.info("extraction.ocr_available", languages=self.ocr_languages)
        except Exception as exc:
            self._ocr_available = False
            logger.warning(
                "extraction.ocr_unavailable",
                error=str(exc),
                hint="Install Tesseract and set SMARTTENDER_EXTRACTION__TESSERACT_CMD.",
            )
        return self._ocr_available

    def _ocr_pages(self, content: bytes, page_indices: list[int]) -> dict[int, str]:
        """Rasterise and OCR the given pages.

        pypdfium2 renders without needing Poppler, which keeps this working
        identically on Windows and in the container.
        """
        results: dict[int, str] = {}
        try:
            import pypdfium2 as pdfium
            import pytesseract
        except ImportError:  # pragma: no cover
            return results

        scale = self.ocr_dpi / 72.0
        document = None
        try:
            document = pdfium.PdfDocument(content)
            for index in page_indices:
                try:
                    page = document[index]
                    bitmap = page.render(scale=scale, grayscale=True)
                    image = bitmap.to_pil()
                    image = self._preprocess(image)
                    results[index] = pytesseract.image_to_string(
                        image, lang=self.ocr_languages
                    )
                except Exception as exc:
                    logger.debug("extraction.ocr_page_failed", page=index, error=str(exc))
        except Exception as exc:
            logger.warning("extraction.ocr_failed", error=str(exc))
        finally:
            if document is not None:
                try:
                    document.close()
                except Exception:
                    pass
        return results

    def _preprocess(self, image: Any) -> Any:
        """Clean a rasterised page before OCR.

        Adaptive thresholding plus a median blur is the standard recipe for
        scanned administrative documents: it removes the grey cast and the
        speckle that scanners and stamps introduce, which is worth several
        percent of character accuracy on exactly the paperwork tenders attach.
        Falls back to the untouched image if OpenCV is unavailable.
        """
        try:
            import cv2
            import numpy as np
            from PIL import Image

            array = np.array(image.convert("L"))
            array = cv2.medianBlur(array, 3)
            array = cv2.adaptiveThreshold(
                array, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 10
            )
            return Image.fromarray(array)
        except Exception:
            return image

    # ------------------------------------------------------------------
    def _extract_docx(self, content: bytes) -> ExtractedText:
        try:
            import docx
        except ImportError:  # pragma: no cover
            return ExtractedText(method="none", error="python-docx is not installed.")

        try:
            document = docx.Document(io.BytesIO(content))
        except Exception as exc:
            return ExtractedText(method="none", error=f"Unreadable DOCX: {exc}")

        parts = [p.text for p in document.paragraphs if p.text and p.text.strip()]
        # Requirements in tender documents live in tables far more often than
        # in paragraphs, so skipping them would lose the most valuable content.
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                if cells:
                    parts.append(" | ".join(cells))

        text, truncated = clean_extracted_text("\n".join(parts), max_chars=self.max_chars)
        return ExtractedText(
            text=text,
            method="digital" if text else "none",
            truncated=truncated,
            error=None if text else "No readable text found in the DOCX.",
        )

    def _extract_html(self, content: bytes) -> ExtractedText:
        try:
            from app.connectors.parsing.selectors import parse_html

            soup = parse_html(content)
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            raw = soup.get_text("\n", strip=True)
        except Exception as exc:
            return ExtractedText(method="none", error=f"Unreadable HTML: {exc}")

        text, truncated = clean_extracted_text(raw, max_chars=self.max_chars)
        return ExtractedText(
            text=text,
            method="digital" if text else "none",
            truncated=truncated,
            error=None if text else "No readable text found in the HTML.",
        )


_extractor: DocumentExtractor | None = None


def get_extractor() -> DocumentExtractor:
    global _extractor
    if _extractor is None:
        _extractor = DocumentExtractor()
    return _extractor


def reset_extractor() -> None:
    """Drop the cached extractor — used by tests that change settings."""
    global _extractor
    _extractor = None
