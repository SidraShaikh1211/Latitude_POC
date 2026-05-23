import json
from datetime import date

import pytest

from app.pas.bundle_parser import BundleParseError, parse_pas_bundle
from app.settings import PROJECT_ROOT


SMITH_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "smith_claim_bundle.json"


@pytest.fixture
def smith_bundle() -> dict:
    if not SMITH_FIXTURE.exists():
        pytest.skip(f"Smith fixture not generated: {SMITH_FIXTURE}")
    return json.loads(SMITH_FIXTURE.read_text())


def test_parses_smith_case_context(smith_bundle):
    parsed = parse_pas_bundle(smith_bundle)
    ctx = parsed.context
    assert ctx.cpt_code == "62323"
    assert "M54.16" in ctx.icd10_codes
    assert "M79.18" in ctx.icd10_codes
    assert "M47.816" in ctx.icd10_codes
    assert ctx.payer_id == "molina"
    assert ctx.line_of_business == "medicaid"
    assert ctx.state == "NY"
    assert ctx.service_date == date(2026, 4, 8)
    # Smith DOB 1975-11-02; service 2026-04-08 → age 50
    assert ctx.patient_age == 50
    assert ctx.urgency == "standard"
    assert ctx.request_category == "procedural"


def test_parses_smith_facts(smith_bundle):
    parsed = parse_pas_bundle(smith_bundle)
    f = parsed.facts
    assert f.patient is not None
    assert f.patient["birthDate"] == "1975-11-02"
    assert f.coverage is not None
    assert f.coverage["subscriberId"] == "KF464W"
    assert f.claim is not None
    assert f.claim["use"] == "preauthorization"
    assert f.service_request is not None
    # Initial branch: no prior Procedures
    assert f.procedures_prior == []
    # DocumentReference + Binary present
    assert len(f.documents) == 1
    assert len(f.binaries) == 1
    # Binary decoded back to a real PDF
    binary_bytes = next(iter(f.binaries.values()))
    assert binary_bytes.startswith(b"%PDF")


def test_rejects_non_preauth_claim(smith_bundle):
    smith_bundle["entry"][0]["resource"]["use"] = "claim"
    with pytest.raises(BundleParseError, match="preauthorization"):
        parse_pas_bundle(smith_bundle)


def test_rejects_missing_required_resource(smith_bundle):
    # Drop the Coverage entry
    smith_bundle["entry"] = [
        e for e in smith_bundle["entry"]
        if e["resource"]["resourceType"] != "Coverage"
    ]
    with pytest.raises(BundleParseError, match="Coverage"):
        parse_pas_bundle(smith_bundle)


def test_repeat_branch_with_prior_procedure():
    """A Bundle that includes a prior ESI Procedure should surface it under procedures_prior."""
    base = json.loads(SMITH_FIXTURE.read_text())
    base["entry"].append({
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
            "subject": {"reference": "Patient/smith"},
            "performedDateTime": "2026-01-15",
        },
    })
    parsed = parse_pas_bundle(base)
    assert len(parsed.facts.procedures_prior) == 1
    assert parsed.facts.procedures_prior[0]["id"] == "prior-esi-001"
