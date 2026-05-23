"""Unit tests for app/pas/bundle_constructor.py — verifies that a synthetic
(non-Smith) SubmissionData produces a valid R4 PAS Bundle parseable by the
existing bundle_parser."""

import pytest

from app.pas.bundle_constructor import (
    ICD10Code,
    SubmissionData,
    build_pas_bundle,
)
from app.pas.bundle_parser import parse_pas_bundle


SAMPLE_PDF = b"%PDF-1.4\n%fake pdf bytes for unit test\n%%EOF\n"


def _make_submission(**overrides) -> SubmissionData:
    base = SubmissionData(
        patient_given="Jane",
        patient_family="Doe",
        patient_dob="1980-05-15",
        patient_gender="female",
        patient_state="CA",
        payer_id="molina",
        payer_display="Molina Healthcare of California",
        member_id="XY999",
        line_of_business="medicaid",
        plan_name="Molina Medicaid CA",
        cpt_code="62321",
        cpt_display="Cervical interlaminar ESI without imaging guidance",
        service_date="2026-06-01",
        icd10_codes=[
            ICD10Code("M50.12", "Cervical disc disorder with radiculopathy, mid-cervical region", "primary"),
        ],
        body_site_display="Cervical — C5/C6",
        pdf_bytes=SAMPLE_PDF,
        pdf_filename="jane_doe.pdf",
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


def test_build_pas_bundle_returns_valid_r4():
    sub = _make_submission()
    bundle, case_id = build_pas_bundle(sub)
    assert bundle["resourceType"] == "Bundle"
    assert bundle["type"] == "collection"
    assert case_id.startswith("sub-")
    assert len(bundle["entry"]) == 9


def test_build_pas_bundle_uses_provided_case_id():
    sub = _make_submission()
    bundle, case_id = build_pas_bundle(sub, case_id="custom-case-001")
    assert case_id == "custom-case-001"
    # Resource IDs derive from case_id
    patient_entry = next(e for e in bundle["entry"]
                         if e["resource"]["resourceType"] == "Patient")
    assert patient_entry["resource"]["id"] == "patient-custom-case-001"


def test_build_pas_bundle_round_trips_through_parser():
    """Build a bundle and pass it through bundle_parser — the parser must extract
    the same CPT / ICD-10 / payer / age values we put in."""
    sub = _make_submission()
    bundle, case_id = build_pas_bundle(sub, case_id="rt-001")
    parsed = parse_pas_bundle(bundle)
    ctx = parsed.context
    assert ctx.cpt_code == "62321"
    assert "M50.12" in ctx.icd10_codes
    assert ctx.payer_id == "molina"
    assert ctx.line_of_business == "medicaid"
    assert ctx.state == "CA"
    # Jane DOB 1980-05-15, service 2026-06-01 → age 46
    assert ctx.patient_age == 46
    assert parsed.facts.coverage["subscriberId"] == "XY999"
    # Smith uses initial branch; this synthetic case has no prior procedures
    assert parsed.facts.procedures_prior == []
    # Binary present + decoded back to the PDF bytes
    assert len(parsed.facts.binaries) == 1
    bytes_back = next(iter(parsed.facts.binaries.values()))
    assert bytes_back == SAMPLE_PDF


def test_diagnoses_multiple_kinds():
    sub = _make_submission(
        icd10_codes=[
            ICD10Code("M54.16", "Lumbar radiculopathy", "primary"),
            ICD10Code("M79.18", "Other myalgia", "secondary"),
            ICD10Code("M47.816", "Lumbar spondylosis", "secondary"),
        ],
    )
    bundle, _ = build_pas_bundle(sub)
    claim = next(e["resource"] for e in bundle["entry"]
                 if e["resource"]["resourceType"] == "Claim")
    assert len(claim["diagnosis"]) == 3
    primary_codings = [d for d in claim["diagnosis"]
                       if d["type"][0]["coding"][0]["code"] == "principal"]
    assert len(primary_codings) == 1
    assert primary_codings[0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "M54.16"


def test_lob_maps_to_v3_actcode():
    for lob, expected in [
        ("medicaid", "Medicaid"),
        ("medicare-advantage", "Medicare"),
        ("commercial", "Commercial"),
    ]:
        sub = _make_submission(line_of_business=lob)
        bundle, _ = build_pas_bundle(sub)
        coverage = next(e["resource"] for e in bundle["entry"]
                         if e["resource"]["resourceType"] == "Coverage")
        assert coverage["type"]["coding"][0]["code"] == expected


def test_dict_icd10_input_also_works():
    """ICD-10 inputs as dicts (the form-serialization shape) are normalized."""
    bundle, _ = build_pas_bundle(_make_submission(
        icd10_codes=[
            {"code": "M54.16", "display": "Lumbar radiculopathy", "kind": "primary"},
            {"code": "M79.18", "display": "Other myalgia"},   # default kind=secondary
        ],
    ))
    claim = next(e["resource"] for e in bundle["entry"]
                 if e["resource"]["resourceType"] == "Claim")
    assert len(claim["diagnosis"]) == 2
