"""Tests for app/pas/bundle_parser.py — uses synthetic Bundles built via
build_pas_bundle() rather than a pre-baked fixture. The point is to test
the parser's behavior, not to test against frozen Smith data."""

from datetime import date

import pytest
from structlog.testing import capture_logs

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


def _find_claim_item(bundle: dict) -> dict:
    for entry in bundle["entry"]:
        if entry["resource"]["resourceType"] == "Claim":
            return entry["resource"]["item"][0]
    raise AssertionError("bundle has no Claim resource")


def test_requested_indication_codes_resolved_from_diagnosis_sequence(synthetic_bundle):
    """The happy path: item.diagnosisSequence points at every Claim.diagnosis
    row, so the indication list mirrors the full ICD-10 list."""
    parsed = parse_pas_bundle(synthetic_bundle)
    ctx = parsed.context
    assert ctx.requested_indication_icd10_codes == [
        "M54.16", "M79.18", "M47.816"
    ]
    # Full problem list still populated for downstream context.
    assert ctx.icd10_codes == ["M54.16", "M79.18", "M47.816"]


def test_requested_indication_narrows_to_linked_diagnoses(synthetic_bundle):
    """When diagnosisSequence points at a subset, only those codes are
    treated as the requested indication — past-history codes stay out of
    selection but remain on `icd10_codes` for audit."""
    _find_claim_item(synthetic_bundle)["diagnosisSequence"] = [1]
    parsed = parse_pas_bundle(synthetic_bundle)
    assert parsed.context.requested_indication_icd10_codes == ["M54.16"]
    assert parsed.context.icd10_codes == ["M54.16", "M79.18", "M47.816"]


def test_rejects_missing_diagnosis_sequence(synthetic_bundle):
    """No diagnosisSequence on the requested item → hard parse error."""
    item = _find_claim_item(synthetic_bundle)
    item.pop("diagnosisSequence", None)
    with pytest.raises(BundleParseError, match="diagnosisSequence is required"):
        parse_pas_bundle(synthetic_bundle)


def test_rejects_empty_diagnosis_sequence(synthetic_bundle):
    """Empty diagnosisSequence is treated the same as missing."""
    _find_claim_item(synthetic_bundle)["diagnosisSequence"] = []
    with pytest.raises(BundleParseError, match="diagnosisSequence is required"):
        parse_pas_bundle(synthetic_bundle)


def test_rejects_unresolved_diagnosis_sequence(synthetic_bundle):
    """diagnosisSequence pointing at a non-existent diagnosis row errors
    rather than silently producing an empty indication list."""
    _find_claim_item(synthetic_bundle)["diagnosisSequence"] = [99]
    with pytest.raises(BundleParseError, match=r"\[99\]"):
        parse_pas_bundle(synthetic_bundle)


def _drop_service_requests(bundle: dict) -> None:
    bundle["entry"] = [
        e for e in bundle["entry"]
        if e["resource"]["resourceType"] != "ServiceRequest"
    ]


def _find_service_request(bundle: dict) -> dict:
    for entry in bundle["entry"]:
        if entry["resource"]["resourceType"] == "ServiceRequest":
            return entry["resource"]
    raise AssertionError("bundle has no ServiceRequest resource")


def _find_claim(bundle: dict) -> dict:
    for entry in bundle["entry"]:
        if entry["resource"]["resourceType"] == "Claim":
            return entry["resource"]
    raise AssertionError("bundle has no Claim resource")


def test_request_category_inferred_from_surgical_cpt_when_no_service_request():
    """No ServiceRequest in the bundle → category derives from CPT inference,
    not from a silent default to 'procedural'."""
    bundle, _ = build_pas_bundle(
        _make_submission(cpt_code="58150", cpt_display="Total abdominal hysterectomy"),
        case_id="hys-no-sr",
    )
    _drop_service_requests(bundle)
    parsed = parse_pas_bundle(bundle)
    assert parsed.context.request_category == "surgical"


def test_request_category_inferred_from_pharmacy_cpt_when_no_service_request():
    bundle, _ = build_pas_bundle(
        _make_submission(cpt_code="tirzepatide", cpt_display="Zepbound"),
        case_id="zep-no-sr",
    )
    _drop_service_requests(bundle)
    parsed = parse_pas_bundle(bundle)
    assert parsed.context.request_category == "pharmacy"


def test_request_category_recognized_from_snomed_only_coding(synthetic_bundle):
    """A ServiceRequest carrying only a SNOMED `coding.code` (no recognizable
    `text`) still maps to our enum via SNOMED_TO_CATEGORY."""
    sr = _find_service_request(synthetic_bundle)
    sr["category"] = [{
        "coding": [{
            "system": "http://snomed.info/sct",
            "code": "387713003",
            "display": "Surgical procedure",
        }],
    }]
    parsed = parse_pas_bundle(synthetic_bundle)
    assert parsed.context.request_category == "surgical"


def test_request_category_unrecognized_sender_text_falls_through_to_inference(synthetic_bundle):
    """Sender writes a category we don't understand ('radiology') and uses a
    SNOMED code we don't map — we infer from CPT instead of defaulting to
    'procedural'. With CPT 62323 (ESI) the inference is 'procedural'."""
    sr = _find_service_request(synthetic_bundle)
    sr["category"] = [{
        "text": "radiology",
        "coding": [{
            "system": "http://snomed.info/sct",
            "code": "999999999",
            "display": "Unknown",
        }],
    }]
    parsed = parse_pas_bundle(synthetic_bundle)
    # CPT 62323 is outside surgical ranges and not pharmacy → 'procedural'.
    assert parsed.context.request_category == "procedural"


def test_request_category_sender_wins_on_disagreement_and_logs_warning():
    """Sender declares 'procedural' on a surgical CPT (58150). Sender wins
    (clinical context > heuristic) but the divergence is logged."""
    bundle, _ = build_pas_bundle(
        _make_submission(cpt_code="58150", cpt_display="Hysterectomy"),
        case_id="hys-mismatch",
    )
    sr = _find_service_request(bundle)
    # Force the sender to declare 'procedural' despite a surgical CPT.
    sr["category"] = [{"text": "procedural"}]

    with capture_logs() as logs:
        parsed = parse_pas_bundle(bundle)

    assert parsed.context.request_category == "procedural"
    disagree = [e for e in logs if e.get("event") == "bundle.request_category_disagrees"]
    assert disagree, f"expected disagreement warning, got: {logs}"
    entry = disagree[0]
    assert entry["sender"] == "procedural"
    assert entry["inferred"] == "surgical"
    assert entry["cpt_code"] == "58150"


def test_cpt_display_and_indication_displays_and_body_site_round_trip(synthetic_bundle):
    """Constructor writes CPT display, ICD-10 displays, and body site; parser
    must surface all three on CaseContext so the digest's REQUESTED SERVICE
    block has the human-readable surface it needs.
    """
    parsed = parse_pas_bundle(synthetic_bundle)
    ctx = parsed.context
    assert ctx.cpt_display == "Lumbar interlaminar ESI with imaging guidance"
    assert ctx.indication_displays == {
        "M54.16": "Radiculopathy, lumbar region",
        "M79.18": "Other myalgia",
        "M47.816": "Lumbar spondylosis",
    }
    assert ctx.body_site == "Lumbar — L4/5"


def test_cpt_display_falls_back_to_coding_display(synthetic_bundle):
    """When productOrService has no .text the parser falls back to the first
    coding's display."""
    item = _find_claim_item(synthetic_bundle)
    item["productOrService"].pop("text", None)
    parsed = parse_pas_bundle(synthetic_bundle)
    assert parsed.context.cpt_display == "Lumbar interlaminar ESI with imaging guidance"


def test_cpt_display_empty_when_no_text_or_display(synthetic_bundle):
    """No .text, no coding[].display → empty string, not a raised error."""
    item = _find_claim_item(synthetic_bundle)
    item["productOrService"].pop("text", None)
    for c in item["productOrService"].get("coding") or []:
        c.pop("display", None)
    parsed = parse_pas_bundle(synthetic_bundle)
    assert parsed.context.cpt_display == ""


def test_indication_displays_omits_codes_without_display(synthetic_bundle):
    """A Claim.diagnosis row with no .text and no coding[].display is
    skipped from indication_displays rather than mapped to an empty string."""
    claim = _find_claim(synthetic_bundle)
    # Strip display from the secondary diagnosis row (M79.18 at sequence 2).
    for dx in claim.get("diagnosis") or []:
        if dx.get("sequence") == 2:
            concept = dx.get("diagnosisCodeableConcept") or {}
            concept.pop("text", None)
            for c in concept.get("coding") or []:
                c.pop("display", None)
    parsed = parse_pas_bundle(synthetic_bundle)
    displays = parsed.context.indication_displays
    assert "M54.16" in displays
    assert "M47.816" in displays
    assert "M79.18" not in displays


def test_body_site_empty_when_claim_and_service_request_lack_it(synthetic_bundle):
    """No bodySite anywhere → empty string (digest will omit the line)."""
    item = _find_claim_item(synthetic_bundle)
    item.pop("bodySite", None)
    sr = _find_service_request(synthetic_bundle)
    sr.pop("bodySite", None)
    parsed = parse_pas_bundle(synthetic_bundle)
    assert parsed.context.body_site == ""


def test_body_site_falls_back_to_service_request(synthetic_bundle):
    """Claim has no bodySite but ServiceRequest does → use the SR text."""
    item = _find_claim_item(synthetic_bundle)
    item.pop("bodySite", None)
    parsed = parse_pas_bundle(synthetic_bundle)
    assert parsed.context.body_site == "Lumbar — L4/5"


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
