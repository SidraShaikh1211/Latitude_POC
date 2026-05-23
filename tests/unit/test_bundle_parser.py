"""Tests for app/pas/bundle_parser.py — uses synthetic Bundles built via
build_pas_bundle() rather than a pre-baked fixture. The point is to test
the parser's behavior, not to test against frozen Smith data."""

from datetime import date

import pytest

from app.pas.bundle_constructor import ICD10Code, SubmissionData, build_pas_bundle
from app.pas.bundle_parser import BundleParseError, parse_pas_bundle


SAMPLE_PDF = b"%PDF-1.4\n%fake pdf bytes for unit test\n%%EOF\n"


def _make_submission(**overrides) -> SubmissionData:
    base = SubmissionData(
        patient_given="Test",
        patient_family="Patient",
        patient_dob="1975-11-02",
        patient_gender="male",
        patient_state="NY",
        payer_id="molina",
        payer_display="Molina Healthcare of New York",
        member_id="KF464W",
        line_of_business="medicaid",
        plan_name="Molina Medicaid NY",
        cpt_code="62323",
        cpt_display="Lumbar interlaminar ESI with imaging guidance",
        service_date="2026-04-08",
        icd10_codes=[
            ICD10Code("M54.16", "Radiculopathy, lumbar region", "primary"),
            ICD10Code("M79.18", "Other myalgia", "secondary"),
            ICD10Code("M47.816", "Lumbar spondylosis", "secondary"),
        ],
        body_site_display="Lumbar — L4/5",
        pdf_bytes=SAMPLE_PDF,
        pdf_filename="test.pdf",
        submission_timestamp="2026-04-01T09:00:00-04:00",  # before service_date for deterministic urgency
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    return base


@pytest.fixture
def synthetic_bundle() -> dict:
    bundle, _ = build_pas_bundle(_make_submission(), case_id="parser-test-001")
    return bundle


def test_parses_case_context(synthetic_bundle):
    parsed = parse_pas_bundle(synthetic_bundle)
    ctx = parsed.context
    assert ctx.cpt_code == "62323"
    assert "M54.16" in ctx.icd10_codes
    assert "M79.18" in ctx.icd10_codes
    assert "M47.816" in ctx.icd10_codes
    assert ctx.payer_id == "molina"
    assert ctx.line_of_business == "medicaid"
    assert ctx.state == "NY"
    assert ctx.service_date == date(2026, 4, 8)
    # DOB 1975-11-02; service 2026-04-08 → age 50
    assert ctx.patient_age == 50
    assert ctx.urgency == "standard"
    assert ctx.request_category == "procedural"


def test_parses_facts(synthetic_bundle):
    parsed = parse_pas_bundle(synthetic_bundle)
    f = parsed.facts
    assert f.patient is not None
    assert f.patient["birthDate"] == "1975-11-02"
    assert f.coverage is not None
    assert f.coverage["subscriberId"] == "KF464W"
    assert f.claim is not None
    assert f.claim["use"] == "preauthorization"
    assert f.service_request is not None
    # Initial branch: no prior Procedures included by the constructor
    assert f.procedures_prior == []
    # DocumentReference + Binary present
    assert len(f.documents) == 1
    assert len(f.binaries) == 1
    # Binary decoded back to the bytes we put in
    binary_bytes = next(iter(f.binaries.values()))
    assert binary_bytes == SAMPLE_PDF


def test_rejects_non_preauth_claim(synthetic_bundle):
    # Mutate the Claim's `use` to something other than preauthorization
    for entry in synthetic_bundle["entry"]:
        if entry["resource"]["resourceType"] == "Claim":
            entry["resource"]["use"] = "claim"
            break
    with pytest.raises(BundleParseError, match="preauthorization"):
        parse_pas_bundle(synthetic_bundle)


def test_rejects_missing_required_resource(synthetic_bundle):
    # Drop the Coverage entry
    synthetic_bundle["entry"] = [
        e for e in synthetic_bundle["entry"]
        if e["resource"]["resourceType"] != "Coverage"
    ]
    with pytest.raises(BundleParseError, match="Coverage"):
        parse_pas_bundle(synthetic_bundle)


def test_repeat_branch_with_prior_procedure():
    """A Bundle that includes a prior ESI Procedure should surface it under
    procedures_prior."""
    bundle, _ = build_pas_bundle(_make_submission(), case_id="repeat-test-001")
    bundle["entry"].append({
        "fullUrl": "urn:uuid:prior-esi-001",
        "resource": {
            "resourceType": "Procedure",
            "id": "prior-esi-001",
            "status": "completed",
            "code": {
                "coding": [{
                    "system": "http://www.ama-assn.org/go/cpt",
                    "code": "62323",
                    "display": "Lumbar ESI with imaging guidance",
                }],
            },
            "subject": {"reference": "Patient/patient-repeat-test-001"},
            "performedDateTime": "2026-01-15",
        },
    })
    parsed = parse_pas_bundle(bundle)
    assert len(parsed.facts.procedures_prior) == 1
    assert parsed.facts.procedures_prior[0]["id"] == "prior-esi-001"
