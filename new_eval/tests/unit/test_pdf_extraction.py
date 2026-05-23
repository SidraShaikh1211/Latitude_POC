from app.extraction.pdf import extract_pdf, find_quote_location
from app.settings import PROJECT_ROOT


SMITH_PDF = PROJECT_ROOT / "clinical_pdfs" / "David_Smith_Clinical.pdf"


def test_extracts_smith_pdf():
    doc = extract_pdf(SMITH_PDF)
    assert doc.page_count == 23
    assert doc.sha256
    assert len(doc.full_text) > 30_000
    # No empty pages
    for pt in doc.pages:
        assert pt.normalized != ""
        assert pt.char_count > 0


def test_excerpt_with_offsets():
    doc = extract_pdf(SMITH_PDF)
    pt = doc.page(1)
    assert pt is not None
    snippet = doc.excerpt(1, 0, 50)
    assert isinstance(snippet, str)
    assert len(snippet) > 0


def test_find_quote_location_known_string():
    doc = extract_pdf(SMITH_PDF)
    # The Smith doc has a fax header on every page; this is a substring of it.
    loc = find_quote_location(doc, "Fax Server")
    assert loc is not None
    page, start, end = loc
    assert 1 <= page <= 23
    assert end > start
