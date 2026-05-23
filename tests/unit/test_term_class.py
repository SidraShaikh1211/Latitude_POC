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


# --- Gyn / hormonal therapy class tests ---

def test_mirena_is_progesterone_iud():
    r = lookup_medication_class("Mirena", "progesterone_iud")
    assert r.in_class
    assert r.source == "dictionary"


def test_mirena_is_hormonal_therapy_umbrella():
    r = lookup_medication_class("Mirena", "hormonal_therapy")
    assert r.in_class


def test_dmpa_is_injectable_hormone():
    r = lookup_medication_class("DMPA", "injectable_hormone")
    assert r.in_class
    assert lookup_medication_class("Depo-Provera", "injectable_hormone").in_class
    assert lookup_medication_class("medroxyprogesterone acetate", "injectable_hormone").in_class


def test_leuprolide_is_gnrh_analog():
    r = lookup_medication_class("leuprolide", "gnrh_analog")
    assert r.in_class
    assert lookup_medication_class("Lupron", "gnrh_analog").in_class
    assert lookup_medication_class("goserelin", "gnrh_analog").in_class


def test_danazol_is_gnrh_analog():
    # danazol is grouped with GnRH analogs in the policy ("agents for inducing amenorrhea")
    assert lookup_medication_class("danazol", "gnrh_analog").in_class


def test_ocp_is_oral_contraceptive():
    assert lookup_medication_class("Yaz", "oral_contraceptive").in_class
    assert lookup_medication_class("Lo Loestrin", "oral_contraceptive").in_class
    assert lookup_medication_class("ethinyl estradiol", "oral_contraceptive").in_class
    assert lookup_medication_class("OCP", "oral_contraceptive").in_class


def test_ibuprofen_is_not_hormonal():
    r = lookup_medication_class("ibuprofen", "hormonal_therapy")
    # ibuprofen is NSAID, not hormonal — but unknown to the dictionary returns "unknown",
    # which we expect since ibuprofen isn't in any hormonal subclass
    assert not r.in_class


def test_n8003_is_adenomyosis():
    r = lookup_icd10_class("N80.03", "adenomyosis")
    assert r.in_class
    assert r.source == "dictionary"


def test_n80_family_is_endometriosis():
    assert lookup_icd10_class("N80.0", "endometriosis").in_class
    assert lookup_icd10_class("N80.1", "endometriosis").in_class
    assert lookup_icd10_class("N80.9", "endometriosis").in_class


def test_n946_is_dysmenorrhea():
    assert lookup_icd10_class("N94.6", "dysmenorrhea").in_class


def test_n92_family_is_abnormal_uterine_bleeding():
    assert lookup_icd10_class("N92.0", "abnormal_uterine_bleeding").in_class
    assert lookup_icd10_class("N93.8", "abnormal_uterine_bleeding").in_class
    assert lookup_icd10_class("N91.5", "abnormal_uterine_bleeding").in_class


def test_classify_icd10_includes_gyn_groupings():
    hits = classify_icd10("N80.03")
    assert "endometriosis" in hits
    assert "adenomyosis" in hits
