"""Tests for the deterministic short-circuit evaluator.

Two contracts to enforce:
  1. When the handler returns a verdict, it must be the right verdict.
  2. When the data is ambiguous OR the kind isn't supported, it must return
     None so the LLM gets a chance.
"""

from datetime import date
from unittest.mock import patch

import pytest

from app.extraction.intake import (
    Citation,
    ExtractedCondition,
    ExtractedFacts,
    ExtractedMedication,
    ExtractedPatient,
    ExtractedProcedure,
)
from app.pas.bundle_parser import FactCollection
from app.policy.deterministic_eval import try_deterministic_verdict
from app.policy.registry import CriterionLeaf, CriterionNode, Exclusion, PolicyCitation
from app.policy.tools import CaseFacts


def _leaf(leaf_id: str, evaluation: dict) -> CriterionLeaf:
    return CriterionLeaf(
        id=leaf_id,
        type="leaf",
        description="",
        policy_citation=PolicyCitation(page=1, section=None, quote="stub"),
        evaluation=evaluation,
        verdict_rubric={"met": "", "not_met": ""},
    )


def _exclusion(ex_id: str, evaluation: dict) -> Exclusion:
    return Exclusion(
        id=ex_id,
        description="",
        policy_citation=PolicyCitation(page=1, section=None, quote="stub"),
        evaluation=evaluation,
        verdict_rubric={"met": ""},
    )


def _case(
    *,
    conditions=None,
    medications=None,
    procedures=None,
    patient=None,
    service_date=None,
) -> CaseFacts:
    extracted = ExtractedFacts(
        patient=patient,
        conditions=conditions or [],
        medications=medications or [],
        procedures=procedures or [],
    )
    return CaseFacts(
        bundle_facts=FactCollection(),
        extracted=extracted,
        service_date=service_date,
    )


# ---------------------------------------------------------------------------
# Unsupported kinds → defer to LLM
# ---------------------------------------------------------------------------


def test_unknown_kind_returns_none():
    leaf = _leaf("c1", {"evaluator_kind": "narrative_check"})
    assert try_deterministic_verdict(leaf, _case()) is None


def test_missing_evaluation_returns_none():
    leaf = _leaf("c1", {})
    assert try_deterministic_verdict(leaf, _case()) is None


# ---------------------------------------------------------------------------
# diagnosis_code (qualifying patterns)
# ---------------------------------------------------------------------------


def _radiculopathy_leaf() -> CriterionLeaf:
    return _leaf("radic", {
        "evaluator_kind": "diagnosis_code",
        "value_constraints": {"icd10_qualifying_patterns": ["M54.1*", "M50.1*"]},
    })


def test_diagnosis_code_met():
    case = _case(conditions=[
        ExtractedCondition(icd10_code="M54.16", display="Lumbar radic"),
    ])
    v = try_deterministic_verdict(_radiculopathy_leaf(), case)
    assert v is not None
    assert v.verdict == "met"
    assert v.confidence >= 0.95
    assert any("M54.16" in pe.value_summary for pe in v.patient_evidence)


def test_diagnosis_code_not_documented_when_codes_dont_match():
    case = _case(conditions=[
        ExtractedCondition(icd10_code="Z00.00", display="Unrelated"),
    ])
    v = try_deterministic_verdict(_radiculopathy_leaf(), case)
    assert v is not None
    assert v.verdict == "not_documented"


def test_diagnosis_code_not_documented_when_no_conditions():
    v = try_deterministic_verdict(_radiculopathy_leaf(), _case())
    assert v is not None
    assert v.verdict == "not_documented"


def test_diagnosis_code_bundle_codes_count():
    bundle = FactCollection()
    bundle.conditions = [{
        "resourceType": "Condition",
        "id": "c-1",
        "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "M50.13"}]},
    }]
    case = CaseFacts(bundle_facts=bundle, extracted=None)
    v = try_deterministic_verdict(_radiculopathy_leaf(), case)
    assert v is not None and v.verdict == "met"


# ---------------------------------------------------------------------------
# diagnosis_code (exclusion / non-qualifying)
# ---------------------------------------------------------------------------


def _non_radic_exclusion() -> Exclusion:
    return _exclusion("X1", {
        "evaluator_kind": "diagnosis_code",
        "value_constraints": {"non_qualifying_primary_icd10": ["M54.5", "M54.50", "M54.9"]},
    })


def test_diagnosis_code_exclusion_fires_when_present():
    case = _case(conditions=[ExtractedCondition(icd10_code="M54.5", display="LBP")])
    v = try_deterministic_verdict(_non_radic_exclusion(), case)
    assert v is not None and v.verdict == "met"


def test_diagnosis_code_exclusion_not_met_when_absent():
    case = _case(conditions=[ExtractedCondition(icd10_code="M54.16", display="Radic")])
    v = try_deterministic_verdict(_non_radic_exclusion(), case)
    assert v is not None and v.verdict == "not_met"


# ---------------------------------------------------------------------------
# numeric_threshold (age)
# ---------------------------------------------------------------------------


def _age18_leaf() -> CriterionLeaf:
    return _leaf("age18", {
        "evaluator_kind": "numeric_threshold",
        "value_constraints": {"age_min": 18},
    })


def test_age_threshold_met():
    case = _case(
        patient=ExtractedPatient(birth_date="2000-01-01"),
        service_date=date(2026, 5, 1),
    )
    v = try_deterministic_verdict(_age18_leaf(), case)
    assert v is not None and v.verdict == "met"
    assert any("age=26" in pe.value_summary for pe in v.patient_evidence)


def test_age_threshold_not_met():
    case = _case(
        patient=ExtractedPatient(birth_date="2010-04-12"),
        service_date=date(2026, 4, 8),
    )
    v = try_deterministic_verdict(_age18_leaf(), case)
    assert v is not None and v.verdict == "not_met"


def test_age_threshold_not_documented_without_birth_date():
    case = _case(service_date=date(2026, 4, 8))
    v = try_deterministic_verdict(_age18_leaf(), case)
    assert v is not None and v.verdict == "not_documented"


def test_age_threshold_not_documented_without_service_date():
    case = _case(patient=ExtractedPatient(birth_date="2000-01-01"))
    v = try_deterministic_verdict(_age18_leaf(), case)
    assert v is not None and v.verdict == "not_documented"


def test_numeric_threshold_without_age_constraint_defers():
    """A non-age numeric_threshold (e.g., NRS) defers to the LLM."""
    leaf = _leaf("nrs", {
        "evaluator_kind": "numeric_threshold",
        "value_constraints": {"nrs_min_exclusive": 4},
    })
    case = _case(patient=ExtractedPatient(birth_date="2000-01-01"),
                 service_date=date(2026, 1, 1))
    assert try_deterministic_verdict(leaf, case) is None


# ---------------------------------------------------------------------------
# class_membership (dictionary-only)
# ---------------------------------------------------------------------------


def _drug_class_leaf() -> CriterionLeaf:
    return _leaf("drug", {
        "evaluator_kind": "class_membership",
        "value_constraints": {
            "qualifying_classes": ["NSAID", "muscle_relaxant", "anticonvulsant"],
        },
    })


def test_class_membership_met_via_dictionary():
    case = _case(medications=[
        ExtractedMedication(resource_type="MedicationStatement", medication_name="ibuprofen"),
    ])
    v = try_deterministic_verdict(_drug_class_leaf(), case)
    assert v is not None and v.verdict == "met"


def test_class_membership_not_documented_no_meds():
    v = try_deterministic_verdict(_drug_class_leaf(), _case())
    assert v is not None and v.verdict == "not_documented"


def test_class_membership_unknown_med_defers():
    """A medication unknown to the dictionary could belong to a qualifying
    class — must defer rather than answer not_met."""
    case = _case(medications=[
        ExtractedMedication(
            resource_type="MedicationStatement", medication_name="some-novel-drug",
        ),
    ])
    assert try_deterministic_verdict(_drug_class_leaf(), case) is None


def test_class_membership_not_met_when_all_meds_dictionary_negative():
    """All meds dictionary-resolve to non-members of every qualifying class.

    The dictionary's explicit non-member coverage today is acetaminophen→NSAID,
    so the only watertight scenario is a leaf restricted to qualifying_classes=NSAID.
    """
    leaf = _leaf("drug-nsaid-only", {
        "evaluator_kind": "class_membership",
        "value_constraints": {"qualifying_classes": ["NSAID"]},
    })
    case = _case(medications=[
        ExtractedMedication(
            resource_type="MedicationStatement", medication_name="acetaminophen",
        ),
    ])
    v = try_deterministic_verdict(leaf, case)
    assert v is not None and v.verdict == "not_met"


def test_class_membership_acetaminophen_against_multi_class_defers():
    """acetaminophen→NSAID is a dictionary-no, but acetaminophen→muscle_relaxant
    is unknown, so the multi-class leaf must defer to the LLM rather than
    answer not_met."""
    case = _case(medications=[
        ExtractedMedication(
            resource_type="MedicationStatement", medication_name="acetaminophen",
        ),
    ])
    assert try_deterministic_verdict(_drug_class_leaf(), case) is None


# ---------------------------------------------------------------------------
# numeric_count (PT sessions, asymmetric: met-only, defer when below threshold)
# ---------------------------------------------------------------------------


def _pt_sessions_leaf(threshold: int = 12) -> CriterionLeaf:
    return _leaf("pt-sessions", {
        "evaluator_kind": "numeric_count",
        "value_constraints": {"min_total_sessions": threshold},
    })


def test_numeric_count_met_when_threshold_cleared():
    procs = [
        ExtractedProcedure(
            cpt_code="97110", display="PT exercise",
            performed_date=f"2026-01-{d:02d}",
        )
        for d in range(1, 13)  # 12 distinct dates
    ]
    case = _case(procedures=procs)
    v = try_deterministic_verdict(_pt_sessions_leaf(12), case)
    assert v is not None and v.verdict == "met"


def test_numeric_count_collapses_same_day_multiple_cpts():
    """One visit with 97110 + 97140 counts as one session, not two."""
    procs = [
        ExtractedProcedure(cpt_code="97110", display="ex", performed_date="2026-01-01"),
        ExtractedProcedure(cpt_code="97140", display="manual", performed_date="2026-01-01"),
    ]
    v = try_deterministic_verdict(_pt_sessions_leaf(2), _case(procedures=procs))
    assert v is None   # only 1 distinct date — defer rather than answer not_met


def test_numeric_count_defers_when_below_threshold():
    """Notes might document additional sessions — never answer not_met here."""
    procs = [
        ExtractedProcedure(cpt_code="97110", display="ex", performed_date="2026-01-01"),
    ]
    assert try_deterministic_verdict(_pt_sessions_leaf(12), _case(procedures=procs)) is None


def test_numeric_count_defers_when_no_pt_procedures():
    """Zero structured PT procedures could still mean lots of PT in notes."""
    assert try_deterministic_verdict(_pt_sessions_leaf(12), _case()) is None


def test_numeric_count_defers_for_other_shapes():
    """max_count_per_region requires body-region grouping — must defer."""
    leaf = _leaf("max-per-region", {
        "evaluator_kind": "numeric_count",
        "value_constraints": {"max_count_per_region": 2},
    })
    procs = [
        ExtractedProcedure(cpt_code="64483", display="ESI L4-L5", performed_date="2025-10-01"),
    ]
    assert try_deterministic_verdict(leaf, _case(procedures=procs)) is None


def test_numeric_count_ignores_non_therapy_cpts():
    procs = [
        ExtractedProcedure(cpt_code="64483", display="injection", performed_date=f"2026-01-{d:02d}")
        for d in range(1, 13)
    ]
    assert try_deterministic_verdict(_pt_sessions_leaf(12), _case(procedures=procs)) is None


# ---------------------------------------------------------------------------
# Integration: adjudicate_all skips the LLM for deterministic leaves
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adjudicate_all_short_circuits_diagnosis_code():
    """A diagnosis_code leaf is resolved deterministically — the LLM
    adjudicate_criterion path must NOT be invoked for it."""
    from app.policy.adjudicator import adjudicate_all

    determ_leaf = _radiculopathy_leaf()
    llm_leaf = _leaf("narrative", {"evaluator_kind": "narrative_check"})
    root = CriterionNode(
        id="root", type="internal", operator="ALL", description="",
        children=[determ_leaf, llm_leaf],
    )
    case = _case(conditions=[ExtractedCondition(icd10_code="M54.16", display="Radic")])

    called_with: list[str] = []

    async def fake_adjudicate(node, case_, *, digest=None, policy=None):
        called_with.append(node.id)
        from app.llm.client import Usage
        from app.policy.adjudicator import AdjudicationResult
        from app.policy.verdict import CriterionVerdict
        return AdjudicationResult(
            verdict=CriterionVerdict(
                criterion_id=node.id, verdict="met", confidence=0.9, reasoning="llm",
            ),
            iterations=1, usage=Usage(), tool_calls=[],
        )

    with patch(
        "app.policy.adjudicator.adjudicate_criterion",
        side_effect=fake_adjudicate,
    ):
        result = await adjudicate_all(
            criteria_root=root, exclusions=[], case=case, leaf_budget_seconds=5.0,
        )

    assert called_with == ["narrative"]   # deterministic leaf skipped the LLM
    assert result.leaf_verdicts["radic"].verdict == "met"
    assert "M54.16" in result.leaf_verdicts["radic"].reasoning
    assert result.leaf_verdicts["narrative"].verdict == "met"
    assert result.iterations["radic"] == 0
