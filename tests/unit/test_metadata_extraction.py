"""Tests for the metadata extractor's post-loop ICD-10 validator.

The validator is the safety net that runs after the agent loop returns. If
Claude ignored the mandatory-lookup instruction and emitted a code/display
pair that disagrees with ICD-10-CM, the validator must auto-correct the
display (and drop invalid codes into missing_fields). The mandate-in-prompt
is the primary defense; this is the belt-and-suspenders check that ensures
the bundle never ships a contradictory code/display pair.

These tests exercise `_validate_and_correct_icd10` directly so they're fast
and don't require an LLM call.
"""

from app.extraction.metadata import (
    CoverageFields,
    ExtractedMetadata,
    PatientFields,
    ServiceRequestFields,
    _ICD10,
    _validate_and_correct_icd10,
)


def _make_meta(icd10_entries: list[_ICD10]) -> ExtractedMetadata:
    return ExtractedMetadata(
        patient=PatientFields(patient_state="OR"),
        coverage=CoverageFields(payer_id="oregon-hca"),
        service_request=ServiceRequestFields(
            cpt_code="58570",
            service_date="2026-05-25",
            icd10_codes=icd10_entries,
        ),
        extraction_notes="",
        missing_fields=[],
    )


def test_validator_overwrites_wrong_display_for_real_code():
    """The exact bug: Claude emits N80.03 (Adenomyosis) but labels it
    'Endometriosis of uterus'. Validator must overwrite the display with
    the canonical ICD-10-CM text."""
    meta = _make_meta([
        _ICD10(code="N80.03", display="Endometriosis of uterus", kind="primary"),
    ])
    _validate_and_correct_icd10(meta)
    entry = meta.service_request.icd10_codes[0]
    assert entry.code == "N80.03"
    assert entry.display == "Adenomyosis of the uterus"
    assert "corrected display for N80.03" in meta.extraction_notes


def test_validator_passes_matching_code_display():
    """When Claude follows the mandate correctly, the validator is a no-op."""
    meta = _make_meta([
        _ICD10(code="M54.16", display="Radiculopathy, lumbar region", kind="primary"),
    ])
    _validate_and_correct_icd10(meta)
    assert meta.service_request.icd10_codes[0].display == "Radiculopathy, lumbar region"
    assert meta.extraction_notes == ""
    assert meta.missing_fields == []


def test_validator_drops_invalid_code_into_missing_fields():
    """A hallucinated code (not in ICD-10-CM) gets dropped and surfaced."""
    meta = _make_meta([
        _ICD10(code="N80.9", display="Endometriosis, unspecified", kind="primary"),
        _ICD10(code="X99.999", display="Made up", kind="secondary"),
    ])
    _validate_and_correct_icd10(meta)
    codes = [e.code for e in meta.service_request.icd10_codes]
    assert codes == ["N80.9"]
    assert "icd10:X99.999" in meta.missing_fields
    assert "not a valid ICD-10-CM code" in meta.extraction_notes


def test_validator_tolerates_minor_display_punctuation_differences():
    """Trailing whitespace / case / punctuation shouldn't trigger a rewrite —
    the validator normalizes before comparing."""
    meta = _make_meta([
        _ICD10(code="M54.16", display="  RADICULOPATHY, lumbar region.  ", kind="primary"),
    ])
    _validate_and_correct_icd10(meta)
    # Display unchanged because normalized comparison says they're equal.
    assert "RADICULOPATHY" in meta.service_request.icd10_codes[0].display
    assert "corrected display" not in meta.extraction_notes


def test_validator_handles_empty_icd_list():
    meta = _make_meta([])
    _validate_and_correct_icd10(meta)
    assert meta.service_request.icd10_codes == []
    assert meta.missing_fields == []
