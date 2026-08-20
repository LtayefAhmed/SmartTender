"""Cutting a tender or a CV into passages that can be embedded.

An embedding model turns a piece of text into one vector. That vector is an
average of everything the text says, which is useful only while the text says
roughly one thing. Our richest tender holds 441 446 characters — a CCTP
demanding SAP S/4HANA, a privacy notice, a penalty schedule and a signature
block — and averaging all of it produces a point that is near nothing in
particular. Chunking is not an optimisation for model limits; it is what keeps
a vector meaningful.

Three properties matter, in this order:

**Provenance.** Every passage records which document it came from, using the
``===== DOCUMENT:`` markers the extractor writes. A chunk from the CCTP states
a requirement; a chunk from DC1 is a form field. Ranking them equally would let
administrative boilerplate outscore the specification, and there is a lot more
boilerplate than specification.

**Boundaries.** Splitting at a fixed offset cuts sentences in half, and half a
requirement embeds as something the requirement does not mean. Passages are cut
at paragraph breaks where one is available, at sentence ends otherwise, and
mid-word only as a last resort.

**Overlap.** A requirement that straddles a boundary would otherwise appear in
neither passage as a whole. A small overlap makes every span of the source
readable in at least one chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.extraction import DOCUMENT_MARKER, document_priority

__all__ = ["Chunk", "chunk_text", "split_by_document"]

#: Roughly 300 tokens of French at ~4 characters per token, comfortably inside
#: the 512-token window of the sentence-transformer models we target. Measured
#: in characters rather than tokens on purpose: chunking must not require
#: loading a tokenizer, or it becomes impossible to test and to reason about.
DEFAULT_CHUNK_CHARS = 1_200

#: Enough to carry a full sentence across a boundary without duplicating so
#: much that the index doubles in size.
DEFAULT_OVERLAP_CHARS = 150

#: Below this, a passage is a heading, a page number or a stray line. Embedding
#: it produces a vector that matches everything weakly and nothing usefully.
MIN_CHUNK_CHARS = 80

_PARAGRAPH = re.compile(r"\n\s*\n")
#: Sentence end followed by whitespace. Deliberately conservative: French
#: procurement text is full of abbreviations ("art.", "n°", "M.") and a greedy
#: rule would cut constantly in the wrong place.
_SENTENCE_END = re.compile(r"(?<=[.!?;:])\s")


@dataclass(slots=True)
class Chunk:
    """One embeddable passage, and where it came from."""

    text: str
    #: Source document name, or ``None`` for text with no document structure
    #: (a publication body, a CV that is a single file).
    document: str | None
    #: 0-based position within the whole source. Makes a chunk addressable and
    #: keeps the reading order recoverable for explanations.
    index: int
    #: 0 when the passage comes from a substantive document, 1 otherwise. Used
    #: to weight a CCTP passage above an administrative form at ranking time.
    priority: int = 1

    @property
    def char_count(self) -> int:
        return len(self.text)


def split_by_document(text: str) -> list[tuple[str | None, str]]:
    """Split merged text back into ``(document name, body)`` pairs.

    The extractor announces each attachment with ``===== DOCUMENT: name``.
    Text with no marker at all — a publication body — comes back as a single
    unnamed section rather than being discarded.
    """
    marker = DOCUMENT_MARKER.strip()
    if marker not in text:
        stripped = text.strip()
        return [(None, stripped)] if stripped else []

    sections: list[tuple[str | None, str]] = []
    # A preamble before the first marker is real text and must not be lost.
    head, _, remainder = text.partition(marker)
    if head.strip():
        sections.append((None, head.strip()))

    for block in remainder.split(marker):
        name, _, body = block.partition("\n")
        body = body.strip()
        if body:
            sections.append((name.strip() or None, body))
    return sections


def _cut_point(text: str, limit: int) -> int:
    """Where to end a passage that may run up to ``limit`` characters.

    Prefers a paragraph break, then a sentence end, then a word boundary. Each
    fallback is worse than the last, which is why they are tried in order
    rather than picking whichever is nearest.
    """
    if len(text) <= limit:
        return len(text)

    window = text[:limit]
    # Only accept a break in the last third: a paragraph break at character 40
    # would produce a 40-character chunk and waste the budget.
    floor = limit // 3

    breaks = [match.end() for match in _PARAGRAPH.finditer(window) if match.end() > floor]
    if breaks:
        return breaks[-1]

    ends = [match.end() for match in _SENTENCE_END.finditer(window) if match.end() > floor]
    if ends:
        return ends[-1]

    space = window.rfind(" ")
    return space if space > floor else limit


def chunk_text(
    text: str,
    *,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    min_chars: int = MIN_CHUNK_CHARS,
) -> list[Chunk]:
    """Cut ``text`` into embeddable passages, one document at a time.

    Documents are never merged into a shared passage: a chunk spanning the end
    of the CCTP and the start of a privacy notice belongs to neither and
    matches poorly against both.
    """
    if not text or not text.strip():
        return []

    # An overlap as large as the passage would advance the cursor by a single
    # character per turn: 5 000 characters became 4 901 near-identical chunks.
    # Capping it at half guarantees each turn consumes at least half a passage.
    overlap = max(0, min(overlap_chars, chunk_chars // 2))

    chunks: list[Chunk] = []
    for document, body in split_by_document(text):
        priority = document_priority(document) if document else 1
        cursor = 0
        while cursor < len(body):
            remaining = body[cursor:]
            end = _cut_point(remaining, chunk_chars)
            passage = remaining[:end].strip()

            # A short passage is kept only when it is the whole of a document:
            # a two-line annex is still worth indexing, a two-line remainder of
            # a long one is a fragment already covered by the overlap.
            if len(passage) >= min_chars or (not chunks_for(chunks, document) and passage):
                chunks.append(
                    Chunk(
                        text=passage,
                        document=document,
                        index=len(chunks),
                        priority=priority,
                    )
                )

            if end >= len(remaining):
                break

            # Step back by the overlap, but always move forward.
            nxt = cursor + max(1, end - overlap)
            # ...then forward again to a word boundary. An overlap landing
            # mid-word opens the next passage with a fragment — "erme13" — that
            # embeds as noise. Searching forward only ever skips a partial word
            # already carried whole by the previous passage. The window keeps a
            # 200-character URL from swallowing the overlap entirely.
            space = body.find(" ", nxt)
            if 0 <= space - nxt <= 40:
                nxt = space + 1
            cursor = nxt

    return chunks


def chunks_for(chunks: list[Chunk], document: str | None) -> list[Chunk]:
    """Those already produced for one document. Used to decide whether a short
    passage is a whole small document or a leftover fragment."""
    return [chunk for chunk in chunks if chunk.document == document]
