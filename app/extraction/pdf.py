"""Deterministic PDF text extraction.

Wraps PyMuPDF (`pymupdf`) to produce a uniform `ExtractedDocument` that the
downstream Section Detector and Intake Agent consume. Every page is captured
twice: as-is (for fidelity to the source) and normalized (for substring
verification of LLM-generated citations).

This layer does NOT call the LLM. It is pure I/O + text manipulation, so it
is fast, deterministic, and unit-testable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from app.llm.citation_verify import normalize


@dataclass
class PageText:
    page: int          # 1-indexed
    text: str          # raw text from PyMuPDF
    normalized: str    # whitespace-collapsed, zero-width stripped
    char_count: int


@dataclass
class ExtractedDocument:
    document_id: str
    source_path: Path
    page_count: int
    pages: list[PageText] = field(default_factory=list)
    sha256: str = ""
    full_text: str = ""           # raw, joined with '\n\n' between pages
    full_normalized: str = ""     # normalized concatenation

    def page(self, n: int) -> PageText | None:
        if 1 <= n <= len(self.pages):
            return self.pages[n - 1]
        return None

    def excerpt(
        self,
        page: int,
        start_offset: int | None = None,
        end_offset: int | None = None,
        context_chars: int = 200,
    ) -> str:
        """Return a slice of the *normalized* page text. If start/end are
        provided, expands by `context_chars` on each side for readable context.
        If start/end are None, returns the full page text (truncated)."""
        pt = self.page(page)
        if pt is None:
            return ""
        if start_offset is None or end_offset is None:
            return pt.normalized
        lo = max(0, start_offset - context_chars)
        hi = min(len(pt.normalized), end_offset + context_chars)
        return pt.normalized[lo:hi]


def extract_pdf(path: str | Path, document_id: str | None = None) -> ExtractedDocument:
    """Read a PDF from disk and return an ExtractedDocument."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    pdf_bytes = path.read_bytes()
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    doc_id = document_id or f"doc_{digest[:12]}"

    pages: list[PageText] = []
    with pymupdf.open(path) as doc:
        for i in range(len(doc)):
            raw = doc[i].get_text()
            norm = normalize(raw)
            pages.append(PageText(
                page=i + 1,
                text=raw,
                normalized=norm,
                char_count=len(raw),
            ))

    full_text = "\n\n".join(p.text for p in pages)
    full_norm = " ".join(p.normalized for p in pages)

    return ExtractedDocument(
        document_id=doc_id,
        source_path=path,
        page_count=len(pages),
        pages=pages,
        sha256=digest,
        full_text=full_text,
        full_normalized=full_norm,
    )


def find_quote_location(
    doc: ExtractedDocument, quote: str
) -> tuple[int, int, int] | None:
    """Locate a quote across all pages. Returns (page, start, end) of the
    first match against normalized text, or None if not found."""
    q = normalize(quote)
    if not q:
        return None
    for pt in doc.pages:
        idx = pt.normalized.find(q)
        if idx >= 0:
            return (pt.page, idx, idx + len(q))
    return None
