"""Tests for the deterministic metadata-vs-intake reconciliation.

Reconciliation runs after intake and before policy selection. It compares the
indication ICD-10s on the requested service (from the inbound PAS Bundle's
Claim.diagnosis + Claim.item.diagnosisSequence) against the codes the intake
extractor pulled from the PDF. The goal is to catch silent miscoding before
the case reaches an adjudicator that will return `not_documented` for
evidence that was, in effect, submitted under a different code.
"""

from datetime import date

from app.extraction.intake import ExtractedCondition, ExtractedFacts
from app.extraction.reconcile import reconcile_metadata_vs_intake
from app.pas.bundle_parser import CaseContext


def _ctx(
    *,
    indications: list[str],
    displays: dict[str, str] | None = None,
) -> CaseContext:
    return CaseContext(
        cpt_code="58571",
        icd10_codes=list(indications),
        payer_id="payer-1",
        line_of_business="commercial",
        state="OR",
        patient_age=42,
        service_date=date(2026, 4, 8),
        requested_indication_icd10_codes=list(indications),
        cpt_display="Laparoscopic total hysterectomy with tubes/ovaries",
        indication_displays=displays or {},
    )


def _facts(*codes: str) -> ExtractedFacts:
    return ExtractedFacts(
        conditions=[
            ExtractedCondition(icd10_code=code, display=f"display for {code}")
            for code in codes
        ]
    )


def test_exact_match_produces_no_warnings():
    ctx = _ctx(indications=["N80.03"], displays={"N80.03": "Adenomyosis of uterus"})
    facts = _facts("N80.03")
    assert reconcile_metadata_vs_intake(ctx, facts) == []


def test_family_match_warns_about_possible_miscoding():
    ctx = _ctx(
        indications=["N80.03"],
        displays={"N80.03": "Adenomyosis of uterus"},
    )
    facts = _facts("N80.9")  # endometriosis, unspecified — same family, different code
    warnings = reconcile_metadata_vs_intake(ctx, facts)
    assert len(warnings) == 1
    w = warnings[0]
    assert "N80.03" in w
    assert "N80.9" in w
    assert "miscoding" in w.lower()
    assert "N80" in w  # the family is named


def test_no_match_warns_no_supporting_condition():
    ctx = _ctx(indications=["N80.03"])
    facts = _facts("Z00.00")  # general adult medical exam — unrelated
    warnings = reconcile_metadata_vs_intake(ctx, facts)
    assert len(warnings) == 1
    w = warnings[0]
    assert "N80.03" in w
    assert "no supporting Condition" in w
    # Unrelated codes should not be cited as "related" in this branch.
    assert "Z00.00" not in w


def test_multiple_primaries_mixed_outcomes():
    ctx = _ctx(
        indications=["N80.03", "N94.6", "K59.00"],
        displays={
            "N80.03": "Adenomyosis of uterus",
            "N94.6": "Dysmenorrhea",
            "K59.00": "Constipation",
        },
    )
    # N80.03 exact match; N94.6 family match (N94.5 present); K59.00 missing entirely.
    facts = _facts("N80.03", "N94.5")
    warnings = reconcile_metadata_vs_intake(ctx, facts)
    assert len(warnings) == 2
    joined = " | ".join(warnings)
    assert "N94.6" in joined and "N94.5" in joined and "miscoding" in joined.lower()
    assert "K59.00" in joined and "no supporting Condition" in joined


def test_empty_intake_conditions_warns_for_every_primary():
    ctx = _ctx(indications=["N80.03", "N94.6"])
    facts = ExtractedFacts(conditions=[])
    warnings = reconcile_metadata_vs_intake(ctx, facts)
    assert len(warnings) == 2
    assert any("N80.03" in w for w in warnings)
    assert any("N94.6" in w for w in warnings)


def test_empty_metadata_primaries_produces_no_warnings():
    ctx = _ctx(indications=[])
    facts = _facts("N80.03", "N94.6")
    assert reconcile_metadata_vs_intake(ctx, facts) == []


def test_case_insensitive_and_whitespace_tolerant_matching():
    ctx = _ctx(indications=["  n80.03  "])
    facts = _facts("N80.03")
    assert reconcile_metadata_vs_intake(ctx, facts) == []


def test_intake_codes_normalized_before_comparison():
    ctx = _ctx(indications=["N80.03"])
    # Intake code in lowercase with stray whitespace — should still match.
    facts = ExtractedFacts(
        conditions=[ExtractedCondition(icd10_code=" n80.03 ", display="Adenomyosis")]
    )
    assert reconcile_metadata_vs_intake(ctx, facts) == []


def test_intake_conditions_without_icd10_code_are_ignored():
    ctx = _ctx(indications=["N80.03"])
    facts = ExtractedFacts(
        conditions=[
            ExtractedCondition(icd10_code=None, display="Pelvic pain (no code)"),
        ]
    )
    warnings = reconcile_metadata_vs_intake(ctx, facts)
    assert len(warnings) == 1
    assert "no supporting Condition" in warnings[0]


def test_duplicate_indications_warn_once():
    ctx = _ctx(indications=["N80.03", "N80.03", "n80.03"])
    facts = _facts("Z00.00")
    warnings = reconcile_metadata_vs_intake(ctx, facts)
    assert len(warnings) == 1


def test_warning_includes_display_text_when_available():
    ctx = _ctx(
        indications=["N80.03"],
        displays={"N80.03": "Adenomyosis of uterus"},
    )
    facts = _facts("Z00.00")
    warnings = reconcile_metadata_vs_intake(ctx, facts)
    assert "Adenomyosis of uterus" in warnings[0]


def test_no_intake_facts_at_all_warns_for_every_primary():
    ctx = _ctx(indications=["N80.03"])
    warnings = reconcile_metadata_vs_intake(ctx, ExtractedFacts())
    assert len(warnings) == 1
    assert "N80.03" in warnings[0]
