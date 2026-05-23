"""Level 2 — Per-criterion eval cases.

Each case is a tuple of (policy criterion id, synthetic ExtractedFacts,
expected verdict, category label). The runner picks the criterion out of
the Molina policy registry and adjudicates against the synthetic facts.

These cover the categories spec'd in the plan (with case counts in
parens — the prototype keeps a representative subset; the framework
scales to the full 30-case target by adding tuples here):

  - categorical/coded (4)
  - numerical threshold (2)
  - temporal/duration (2)
  - synonyms/class (2)
  - negation/partial (2)
  - exclusions (2)

For a full production eval, add cases by following the same pattern.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.extraction.intake import (
    Citation,
    ExtractedAllergy,
    ExtractedCondition,
    ExtractedDiagnosticReport,
    ExtractedFacts,
    ExtractedMedication,
    ExtractedObservation,
    ExtractedPatient,
    ExtractedProcedure,
)
from app.pas.bundle_parser import FactCollection
from app.policy.tools import CaseFacts


@dataclass
class L2Case:
    id: str
    category: str
    criterion_id: str
    facts: ExtractedFacts
    expected_verdict: str
    notes: str = ""
    policy_id: str = "molina-mcp-032"  # default — existing cases stay backward-compatible


def _cit(page: int = 1, quote: str = "stub quote here") -> Citation:
    # Use a stub quote that's long enough to satisfy schema; intake citation
    # verification is bypassed in L2 (facts are synthetic, no document).
    return Citation(document_id="synthetic", page=page, quote=quote)


def _case(facts_kwargs: dict[str, Any]) -> ExtractedFacts:
    """Build an ExtractedFacts with default citations on every resource."""
    return ExtractedFacts(**facts_kwargs)


# ---------------------------------------------------------------------------
# Categorical / coded
# ---------------------------------------------------------------------------

L2_CASES: list[L2Case] = [
    L2Case(
        id="L2-CAT-01",
        category="categorical",
        criterion_id="indication.initial_injection.diagnosis_supported.radicular_pain",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="M54.16", display="Radiculopathy, lumbar region",
                    citations=[_cit(quote="DX Code : M54.16 Radiculopathy, lumbar region")],
                ),
            ],
            "observations": [
                ExtractedObservation(
                    code_display="Straight leg raise — right",
                    value_string="Positive at 35 degrees",
                    citations=[_cit(quote="Positive SLR right at 35 degrees on exam")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="ICD-10 M54.16 + objective exam finding → met",
    ),
    L2Case(
        id="L2-CAT-02",
        category="categorical",
        criterion_id="indication.initial_injection.diagnosis_supported.radicular_pain",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="M54.5", display="Low back pain",
                    citations=[_cit(quote="Low back pain M54.5 documented")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="Axial low back pain only — no radicular features",
    ),
    L2Case(
        id="L2-CAT-03",
        category="categorical",
        criterion_id="indication.initial_injection.diagnosis_supported.herpes_zoster",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="B02.29", display="Other postherpetic nervous system involvement",
                    citations=[_cit(quote="History of herpes zoster B02.29")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="B02.* herpes zoster diagnosis present",
    ),
    L2Case(
        id="L2-CAT-04",
        category="categorical",
        criterion_id="eligibility.age_18_plus",
        facts=_case({"conditions": []}),
        expected_verdict="not_documented",
        notes="No Patient resource → age cannot be computed",
    ),

    # -----------------------------------------------------------------------
    # Numerical threshold
    # -----------------------------------------------------------------------
    L2Case(
        id="L2-NUM-01",
        category="numerical_threshold",
        criterion_id="indication.initial_injection.severity.nrs_above_4",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="Pain intensity (NRS)", value_quantity=8.0, value_unit="/10",
                    citations=[_cit(quote="Pain rating: 8/10 on NRS today")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="NRS 8 > 4 threshold",
    ),
    L2Case(
        id="L2-NUM-02",
        category="numerical_threshold",
        criterion_id="indication.initial_injection.severity.nrs_above_4",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="Pain intensity (NRS)", value_quantity=3.0, value_unit="/10",
                    citations=[_cit(quote="Pain rating: 3/10 on NRS today")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="NRS 3 ≤ 4 threshold",
    ),

    # -----------------------------------------------------------------------
    # Temporal / duration
    # -----------------------------------------------------------------------
    L2Case(
        id="L2-TEMP-01",
        category="temporal",
        criterion_id="indication.initial_injection.conservative_therapy.failure.pt.duration_4_weeks",
        facts=_case({
            "procedures": [
                ExtractedProcedure(
                    cpt_code="97110", display="PT therapeutic exercise",
                    performed_date="2026-01-08",
                    citations=[_cit(quote="PT visit 1 of 12 on 2026-01-08")],
                ),
                ExtractedProcedure(
                    cpt_code="97110", display="PT therapeutic exercise",
                    performed_date="2026-02-10",
                    citations=[_cit(quote="PT visit 12 of 12 on 2026-02-10")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="PT spans 2026-01-08 to 2026-02-10 = ~4.7 weeks ≥ 4 weeks",
    ),
    L2Case(
        id="L2-TEMP-02",
        category="temporal",
        criterion_id="indication.initial_injection.conservative_therapy.failure.pt.duration_4_weeks",
        facts=_case({
            "procedures": [
                ExtractedProcedure(
                    cpt_code="97110", display="PT therapeutic exercise",
                    performed_date="2026-02-01",
                    citations=[_cit(quote="PT initial visit 2026-02-01")],
                ),
                ExtractedProcedure(
                    cpt_code="97110", display="PT therapeutic exercise",
                    performed_date="2026-02-15",
                    citations=[_cit(quote="PT visit 2026-02-15")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="PT spans only ~2 weeks; below 4-week threshold",
    ),

    # -----------------------------------------------------------------------
    # Synonyms / class membership
    # -----------------------------------------------------------------------
    L2Case(
        id="L2-SYN-01",
        category="synonyms",
        criterion_id="indication.initial_injection.conservative_therapy.failure.drug_therapy",
        facts=_case({
            "medications": [
                ExtractedMedication(
                    resource_type="MedicationStatement", medication_name="ibuprofen",
                    dose="600mg", frequency="TID",
                    citations=[_cit(quote="ibuprofen 600mg TID for back pain")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="ibuprofen ∈ NSAID class (dictionary)",
    ),
    L2Case(
        id="L2-SYN-02",
        category="synonyms",
        criterion_id="indication.initial_injection.conservative_therapy.failure.drug_therapy",
        facts=_case({
            "medications": [
                ExtractedMedication(
                    resource_type="MedicationStatement", medication_name="acetaminophen",
                    dose="1g", frequency="QID",
                    citations=[_cit(quote="acetaminophen 1g QID")],
                ),
            ],
        }),
        # acetaminophen is NOT in any qualifying class (NSAID, muscle relaxant, etc.)
        # so the drug_therapy criterion is not_met
        expected_verdict="not_met",
        notes="Acetaminophen alone does NOT satisfy drug_therapy (acetaminophen ∉ qualifying classes)",
    ),

    # -----------------------------------------------------------------------
    # Negation / partial attempts
    # -----------------------------------------------------------------------
    L2Case(
        id="L2-NEG-01",
        category="negation",
        criterion_id="indication.initial_injection.conservative_therapy.failure.pt.sessions_12_total",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="PT plan note", value_string="referral placed but not scheduled",
                    citations=[_cit(quote="Physical therapy referral placed; patient elected to defer pending insurance review.")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="Referral placed but no sessions performed → 0 < 12 sessions. Surface differs from Smith dialect.",
    ),
    L2Case(
        id="L2-NEG-02",
        category="negation",
        criterion_id="indication.initial_injection.conservative_therapy.failure.pt.sessions_12_total",
        facts=_case({
            "procedures": [
                ExtractedProcedure(
                    cpt_code="97110", display="PT therapeutic exercise",
                    performed_date="2026-01-15",
                    citations=[_cit(quote="PT visit 1 — patient stopped after this session")],
                ),
                ExtractedProcedure(
                    cpt_code="97110", display="PT therapeutic exercise",
                    performed_date="2026-01-22",
                    citations=[_cit(quote="PT visit 2 — patient discontinued")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="Only 2 PT sessions, far short of 12",
    ),

    # -----------------------------------------------------------------------
    # Exclusions
    # -----------------------------------------------------------------------
    L2Case(
        id="L2-EXCL-01",
        category="exclusion",
        criterion_id="X2",  # myofascial_pain_syndrome
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="M79.18", display="Other myalgia",
                    clinical_status="active",
                    citations=[_cit(quote="Primary indication: myofascial pain M79.18")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="M79.18 myofascial pain as primary → exclusion fires",
    ),
    L2Case(
        id="L2-EXCL-02",
        category="exclusion",
        criterion_id="X1",  # non_radicular_back_pain
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="M54.16", display="Radiculopathy, lumbar region",
                    citations=[_cit(quote="primary diagnosis radiculopathy lumbar M54.16")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="Radiculopathy is present → non-radicular exclusion does NOT fire",
    ),
]


def make_case_facts(facts: ExtractedFacts) -> CaseFacts:
    """Wrap synthetic ExtractedFacts in the CaseFacts envelope adjudicator expects."""
    return CaseFacts(bundle_facts=FactCollection(), extracted=facts, documents={})


# ---------------------------------------------------------------------------
# Additional Molina ESI cases (verdict-surface coverage)
# ---------------------------------------------------------------------------

L2_CASES.extend([
    L2Case(
        id="L2-ESI-15",
        category="temporal",
        criterion_id="indication.initial_injection.diagnosis_supported.post_surgical_6mo",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="G89.21", display="Chronic pain due to trauma — post-surgical",
                    citations=[_cit(quote="post-laminectomy syndrome G89.21 chronic")],
                ),
            ],
            "procedures": [
                ExtractedProcedure(
                    cpt_code="63030", display="Laminectomy with discectomy L4-L5",
                    performed_date="2024-09-15",
                    citations=[_cit(quote="L4-L5 laminectomy performed 09/15/2024")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="Post-surgical pain ≥6 months post-laminectomy → qualifies for ESI",
    ),
    L2Case(
        id="L2-ESI-16",
        category="temporal",
        criterion_id="indication.initial_injection.diagnosis_supported.post_surgical_6mo",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="G89.21", display="Chronic pain — post-surgical",
                    citations=[_cit(quote="post-surgical pain after recent fusion")],
                ),
            ],
            "procedures": [
                ExtractedProcedure(
                    cpt_code="22612", display="L4-L5 posterior fusion",
                    performed_date="2026-01-10",
                    citations=[_cit(quote="lumbar fusion 01/10/2026")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="Post-surgical, only ~3 months elapsed → fails 6-month wait",
    ),
    L2Case(
        id="L2-ESI-17",
        category="calibration",
        criterion_id="indication.initial_injection.severity.nrs_above_4",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="Pain narrative",
                    value_string="patient describes severe daily pain",
                    citations=[_cit(quote="patient reports severe pain daily, limiting work")],
                ),
            ],
        }),
        expected_verdict="unclear",
        notes="Severe pain narrative but NO discrete NRS Observation → unclear by objective-evidence rule",
    ),
    L2Case(
        id="L2-ESI-18",
        category="calibration",
        criterion_id="indication.initial_injection.conservative_therapy.failure.pt.duration_4_weeks",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="Therapy attempt note",
                    value_string="completed 2 of 12 prescribed sessions before discontinuing",
                    citations=[_cit(quote="Patient completed 2 of 12 prescribed PT sessions before discontinuing the program; symptom flare with extension exercises noted.")],
                ),
            ],
        }),
        expected_verdict="unclear",
        notes="Partial PT trial (2 of 12 sessions) — neither completed nor zero. Tests adjudicator's handling of partial-completion ambiguity. Different surface from Smith.",
    ),
    L2Case(
        id="L2-ESI-19",
        category="calibration",
        criterion_id="indication.initial_injection.conservative_therapy.failure.drug_therapy",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="Med history",
                    value_string="OTC pain reliever tried, agent not specified",
                    citations=[_cit(quote="patient reports OTC pain reliever tried with partial relief")],
                ),
            ],
        }),
        expected_verdict="unclear",
        notes="Generic 'OTC pain reliever' — could be NSAID or acetaminophen; class membership ambiguous",
    ),
])


# ---------------------------------------------------------------------------
# MassHealth Zepbound — L2 cases (10)
# ---------------------------------------------------------------------------

_ZEP = "masshealth-anti-obesity-zepbound"

L2_CASES.extend([
    L2Case(
        id="L2-ZEP-01",
        policy_id=_ZEP,
        category="numerical_threshold",
        criterion_id="eligibility.age_18_plus",
        facts=_case({
            "patient": ExtractedPatient(birth_date="1985-06-10", gender="female",
                                       citations=[_cit(quote="DOB 06/10/1985 female adult")]),
        }),
        expected_verdict="met",
        notes="40yo adult — meets ≥18 requirement",
    ),
    L2Case(
        id="L2-ZEP-02",
        policy_id=_ZEP,
        category="numerical_threshold",
        criterion_id="eligibility.age_18_plus",
        facts=_case({
            "patient": ExtractedPatient(birth_date="2014-02-20", gender="male",
                                       citations=[_cit(quote="pediatric patient DOB 2014-02-20")]),
        }),
        expected_verdict="not_met",
        notes="12yo — fails Zepbound's ≥18 requirement (Wegovy pediatric branch would apply but is not modeled)",
    ),
    L2Case(
        id="L2-ZEP-03",
        policy_id=_ZEP,
        category="numerical_threshold",
        criterion_id="bmi_indication.bmi_30_or_above",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="BMI", value_quantity=34.2, value_unit="kg/m2",
                    effective_date="2026-02-12",
                    citations=[_cit(quote="BMI 34.2 kg/m2 measured 02/12/2026")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="BMI 34.2 ≥30 dated within 90d → meets primary BMI gate",
    ),
    L2Case(
        id="L2-ZEP-04",
        policy_id=_ZEP,
        category="numerical_threshold",
        criterion_id="bmi_indication.bmi_30_or_above",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="BMI", value_quantity=28.5, value_unit="kg/m2",
                    effective_date="2026-02-12",
                    citations=[_cit(quote="BMI 28.5 kg/m2 measured 02/12/2026")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="BMI 28.5 <30 — fails primary BMI gate; must qualify via comorbidity path",
    ),
    L2Case(
        id="L2-ZEP-05",
        policy_id=_ZEP,
        category="composite",
        criterion_id="bmi_indication.bmi_27_with_comorbidity.one_comorbidity",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="E11.9", display="Type 2 diabetes mellitus without complications",
                    clinical_status="active",
                    citations=[_cit(quote="T2DM E11.9 controlled with metformin")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="T2DM is one of the 9 listed comorbidities → comorbidity check met",
    ),
    L2Case(
        id="L2-ZEP-06",
        policy_id=_ZEP,
        category="composite",
        criterion_id="bmi_indication.bmi_27_with_comorbidity.one_comorbidity",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="J45.40", display="Moderate persistent asthma",
                    citations=[_cit(quote="asthma diagnosis J45.40")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="Asthma is NOT in the 9 listed weight-related comorbidities → check fails",
    ),
    L2Case(
        id="L2-ZEP-07",
        policy_id=_ZEP,
        category="temporal",
        criterion_id="phentermine_step_therapy.inadequate_response.adherent_90_of_120",
        facts=_case({
            "medications": [
                ExtractedMedication(
                    resource_type="MedicationStatement", medication_name="phentermine",
                    dose="37.5 mg", frequency="once daily", status="active",
                    start_date="2025-11-01", stop_date="2026-03-01",
                    citations=[_cit(quote="phentermine 37.5 mg daily 11/01/2025 through 03/01/2026")],
                ),
            ],
            "observations": [
                ExtractedObservation(
                    code_display="Pharmacy claims adherence",
                    value_string="95 of last 120 days covered by phentermine fills",
                    citations=[_cit(quote="claims show 95 of last 120 days covered")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="95/120 days ≥ 90 threshold",
    ),
    L2Case(
        id="L2-ZEP-08",
        policy_id=_ZEP,
        category="temporal",
        criterion_id="phentermine_step_therapy.inadequate_response.adherent_90_of_120",
        facts=_case({
            "medications": [
                ExtractedMedication(
                    resource_type="MedicationStatement", medication_name="phentermine",
                    dose="37.5 mg", frequency="once daily",
                    start_date="2026-01-01", stop_date="2026-02-28",
                    citations=[_cit(quote="phentermine 01/01 to 02/28 then discontinued")],
                ),
            ],
            "observations": [
                ExtractedObservation(
                    code_display="Pharmacy claims adherence",
                    value_string="60 of last 120 days covered",
                    citations=[_cit(quote="claims show 60 of last 120 days covered")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="60/120 days < 90 threshold",
    ),
    L2Case(
        id="L2-ZEP-09",
        policy_id=_ZEP,
        category="computed_threshold",
        criterion_id="phentermine_step_therapy.inadequate_response.response_inadequate.insufficient",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="Baseline weight", value_quantity=98.0, value_unit="kg",
                    effective_date="2025-10-15",
                    citations=[_cit(quote="baseline weight 98.0 kg on 10/15/2025 at phentermine start")],
                ),
                ExtractedObservation(
                    code_display="Current weight", value_quantity=96.5, value_unit="kg",
                    effective_date="2026-02-15",
                    citations=[_cit(quote="weight 96.5 kg on 02/15/2026 after 4 months max-dose phentermine")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="Loss 1.5/98 = 1.5% < 5% at 4 months max-dose → insufficient response",
    ),
    L2Case(
        id="L2-ZEP-10",
        policy_id=_ZEP,
        category="substitution",
        criterion_id="phentermine_step_therapy.contraindication",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="I20.9", display="Angina pectoris, unspecified",
                    clinical_status="active",
                    citations=[_cit(quote="documented coronary artery disease with stable angina")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="CAD is on the acceptable phentermine-contraindication list → alternative path satisfied",
    ),
])


# ---------------------------------------------------------------------------
# Oregon Adenomyosis Hysterectomy — L2 cases (10)
# ---------------------------------------------------------------------------

_ORE = "oregon-hcr-39"

L2_CASES.extend([
    L2Case(
        id="L2-ADN-01",
        policy_id=_ORE,
        category="temporal_qual",
        criterion_id="symptoms.duration_6mo_with_qol_impact",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="N94.6", display="Dysmenorrhea, unspecified",
                    clinical_status="active", onset_date="2024-12-15",
                    citations=[_cit(quote="dysmenorrhea N94.6 onset 12/2024 — 14 months")],
                ),
            ],
            "observations": [
                ExtractedObservation(
                    code_display="Quality of life impact",
                    value_string="missing 3 work days per month, sexual dysfunction reported",
                    citations=[_cit(quote="QoL impact: missing 3 work days per month due to pelvic pain")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="Symptoms 14 months + QoL impact documented",
    ),
    L2Case(
        id="L2-ADN-02",
        policy_id=_ORE,
        category="temporal",
        criterion_id="symptoms.duration_6mo_with_qol_impact",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="N94.6", display="Dysmenorrhea",
                    onset_date="2026-02-01",
                    citations=[_cit(quote="recent onset dysmenorrhea last 2 months")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="Only 2 months of symptoms — fails 6-month minimum",
    ),
    L2Case(
        id="L2-ADN-03",
        policy_id=_ORE,
        category="calibration",
        criterion_id="symptoms.duration_6mo_with_qol_impact",
        facts=_case({
            "conditions": [
                ExtractedCondition(
                    icd10_code="N94.6", display="Dysmenorrhea",
                    onset_date="2024-12-15",
                    citations=[_cit(quote="dysmenorrhea, chronic for over a year")],
                ),
            ],
        }),
        expected_verdict="unclear",
        notes="Duration met but no QoL impact documented — partial credit → unclear",
    ),
    L2Case(
        id="L2-ADN-04",
        policy_id=_ORE,
        category="temporal",
        criterion_id="failed_trial.completed.hormonal_6mo",
        facts=_case({
            "medications": [
                ExtractedMedication(
                    resource_type="MedicationStatement",
                    medication_name="norethindrone-ethinyl estradiol",
                    dose="0.5/35 mcg", frequency="once daily",
                    start_date="2024-08-01", stop_date="2025-04-15",
                    status="stopped",
                    citations=[_cit(quote="OCP norethindrone-EE 08/2024 through 04/2025 — 8 months")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="OCP trial 8 months ≥ 6 months",
    ),
    L2Case(
        id="L2-ADN-05",
        policy_id=_ORE,
        category="temporal",
        criterion_id="failed_trial.completed.hormonal_6mo",
        facts=_case({
            "medications": [
                ExtractedMedication(
                    resource_type="MedicationStatement",
                    medication_name="ethinyl estradiol-drospirenone",
                    dose="30 mcg / 3 mg", frequency="once daily",
                    start_date="2025-12-01", stop_date="2026-03-01",
                    status="stopped",
                    citations=[_cit(quote="OCP 12/2025 to 03/2026 — only 3 months trial")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="OCP trial only 3 months — short of 6-month requirement",
    ),
    L2Case(
        id="L2-ADN-06",
        policy_id=_ORE,
        category="class_membership",
        criterion_id="failed_trial.completed.nsaid_therapy",
        facts=_case({
            "medications": [
                ExtractedMedication(
                    resource_type="MedicationStatement", medication_name="ibuprofen",
                    dose="800 mg", frequency="TID",
                    start_date="2024-09-01", stop_date="2025-05-15",
                    citations=[_cit(quote="ibuprofen 800mg TID for dysmenorrhea 09/2024 - 05/2025")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="Ibuprofen ∈ NSAID class, documented across symptomatic period",
    ),
    L2Case(
        id="L2-ADN-07",
        policy_id=_ORE,
        category="imaging_numeric",
        criterion_id="imaging_findings.mri_junctional_zone",
        facts=_case({
            "diagnostic_reports": [
                ExtractedDiagnosticReport(
                    modality="MRI", body_site="Pelvis",
                    findings="Diffuse junctional zone thickening measuring 15 mm. Findings consistent with adenomyosis.",
                    performed_date="2026-01-20",
                    citations=[_cit(quote="MRI pelvis: junctional zone thickening 15 mm consistent with adenomyosis")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="MRI junctional zone 15mm > 12mm threshold",
    ),
    L2Case(
        id="L2-ADN-08",
        policy_id=_ORE,
        category="imaging_numeric",
        criterion_id="imaging_findings.mri_junctional_zone",
        facts=_case({
            "diagnostic_reports": [
                ExtractedDiagnosticReport(
                    modality="MRI", body_site="Pelvis",
                    findings="No findings suggestive of adenomyosis. Junctional zone thickness normal.",
                    performed_date="2026-01-20",
                    citations=[_cit(quote="MRI pelvis: no adenomyosis findings, junctional zone normal")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="MRI explicitly negative for adenomyosis findings",
    ),
    L2Case(
        id="L2-ADN-09",
        policy_id=_ORE,
        category="class_membership",
        criterion_id="cervical_cytology.nonmalignant_if_cervix_present",
        facts=_case({
            "diagnostic_reports": [
                ExtractedDiagnosticReport(
                    modality="Cytology", body_site="Cervix",
                    findings="Negative for intraepithelial lesion or malignancy (NILM). HPV negative.",
                    performed_date="2025-08-12",
                    citations=[_cit(quote="Pap smear NILM, HPV negative, performed 08/12/2025")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="NILM Pap = non-malignant cytology",
    ),
    L2Case(
        id="L2-ADN-10",
        policy_id=_ORE,
        category="temporal",
        criterion_id="pregnancy_status.negative_test",
        facts=_case({
            "observations": [
                ExtractedObservation(
                    code_display="Serum β-hCG", value_string="Negative",
                    effective_date="2026-03-20",
                    citations=[_cit(quote="serum hCG negative on 03/20/2026, 10 days pre-op")],
                ),
            ],
        }),
        expected_verdict="met",
        notes="Negative hCG within 14d window",
    ),
])
