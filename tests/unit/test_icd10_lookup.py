"""Direct tests of the simple-icd-10-cm wrapper used by the metadata
extractor's mandatory ICD lookup tools."""

from app.extraction.icd10_lookup import lookup, search_by_term


# ---------------------------------------------------------------------------
# lookup()
# ---------------------------------------------------------------------------


def test_lookup_known_code_returns_official_description():
    r = lookup("M54.16")
    assert r["valid"] is True
    assert r["official_description"] == "Radiculopathy, lumbar region"


def test_lookup_n80_03_returns_adenomyosis_not_endometriosis():
    """The exact bug that motivated this work: N80.03 is adenomyosis, not
    endometriosis. The wrapper must return the official ICD-10-CM text so
    the metadata extractor cannot silently use a paraphrased label."""
    r = lookup("N80.03")
    assert r["valid"] is True
    assert r["official_description"] == "Adenomyosis of the uterus"


def test_lookup_invalid_code_returns_valid_false():
    r = lookup("NOTACODE")
    assert r["valid"] is False
    assert r["official_description"] is None


def test_lookup_blank_returns_valid_false():
    assert lookup("")["valid"] is False
    assert lookup("   ")["valid"] is False


def test_lookup_strips_whitespace():
    r = lookup("  M54.16  ")
    assert r["valid"] is True


def test_lookup_cpt_code_not_recognized_as_icd():
    """58570 is a CPT (hysterectomy), not an ICD-10 code. The library
    correctly rejects it — this protects the metadata extractor from
    accepting CPTs as diagnoses."""
    r = lookup("58570")
    assert r["valid"] is False


# ---------------------------------------------------------------------------
# search_by_term()
# ---------------------------------------------------------------------------


def test_search_endometriosis_does_not_include_n80_03():
    """The crux of the fix: searching for 'endometriosis' must NOT surface
    N80.03 as a candidate, because N80.03 is adenomyosis per ICD-10-CM."""
    hits = search_by_term("endometriosis", limit=50)
    codes = {h["code"] for h in hits}
    assert "N80.03" not in codes
    # Sanity: the actual endometriosis codes are present
    assert "N80.9" in codes  # "Endometriosis, unspecified"


def test_search_adenomyosis_includes_n80_03():
    hits = search_by_term("adenomyosis", limit=50)
    codes = {h["code"] for h in hits}
    assert "N80.03" in codes


def test_search_empty_term_returns_empty():
    assert search_by_term("") == []
    assert search_by_term("   ") == []


def test_search_returns_dicts_with_code_and_description():
    hits = search_by_term("type 2 diabetes", limit=5)
    assert len(hits) > 0
    for h in hits:
        assert "code" in h and "description" in h
        assert h["code"]
        assert "diabetes" in h["description"].lower()


def test_search_respects_limit():
    hits = search_by_term("endometriosis", limit=3)
    assert len(hits) == 3
