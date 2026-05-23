from app.policy.term_class import (
    classify_icd10,
    classify_medication,
    is_therapy_cpt,
    lookup_icd10_class,
    lookup_medication_class,
)


def test_ibuprofen_is_nsaid():
    r = lookup_medication_class("ibuprofen", "NSAID")
    assert r.in_class
    assert r.source == "dictionary"


def test_advil_brand_resolves_to_nsaid():
    r = lookup_medication_class("Advil", "NSAID")
    assert r.in_class


def test_acetaminophen_is_not_nsaid():
    r = lookup_medication_class("acetaminophen", "NSAID")
    assert not r.in_class
    assert r.source == "dictionary"
    assert "NOT" in r.explanation


def test_tylenol_is_not_nsaid():
    r = lookup_medication_class("Tylenol", "NSAID")
    assert not r.in_class


def test_unknown_medication_returns_unknown():
    r = lookup_medication_class("madeupdrug-xyz", "NSAID")
    assert r.source == "unknown"
    assert not r.in_class


def test_cyclobenzaprine_is_muscle_relaxant():
    assert lookup_medication_class("cyclobenzaprine", "muscle_relaxant").in_class


def test_gabapentin_is_anticonvulsant():
    assert lookup_medication_class("gabapentin", "anticonvulsant").in_class


def test_oxycodone_is_opiate():
    assert lookup_medication_class("oxycodone", "opiate").in_class


def test_apixaban_is_anticoagulant():
    assert lookup_medication_class("apixaban", "anticoagulant").in_class


def test_classify_medication_multi():
    classes = classify_medication("ibuprofen")
    assert "NSAID" in classes


def test_m5416_is_lumbar_radiculopathy():
    r = lookup_icd10_class("M54.16", "lumbar_radiculopathy")
    assert r.in_class
    assert r.source == "dictionary"


def test_m7918_is_myofascial():
    r = lookup_icd10_class("M79.18", "myofascial_pain")
    assert r.in_class


def test_m7918_is_not_radiculopathy():
    r = lookup_icd10_class("M79.18", "lumbar_radiculopathy")
    assert not r.in_class


def test_m545_is_non_radicular():
    r = lookup_icd10_class("M54.5", "non_radicular_back_pain")
    assert r.in_class


def test_b022_is_herpes_zoster():
    r = lookup_icd10_class("B02.2", "herpes_zoster")
    assert r.in_class


def test_classify_icd10_multi():
    hits = classify_icd10("M54.16")
    assert "lumbar_radiculopathy" in hits


def test_classify_icd10_no_match():
    assert classify_icd10("Z99.99") == []


def test_therapy_cpt_recognized():
    assert is_therapy_cpt("97110")
    assert is_therapy_cpt("97140")
    assert not is_therapy_cpt("99213")
