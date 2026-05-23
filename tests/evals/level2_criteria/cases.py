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
    ExtractedCondition,
    ExtractedFacts,
    ExtractedMedication,
    ExtractedObservation,
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
                    code_display="PT note", value_string="patient too painful to start PT",
                    citations=[_cit(quote="PT was too painful to start; patient declined further sessions")],
                ),
            ],
        }),
        expected_verdict="not_met",
        notes="PT not started → 0 < 12 sessions",
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
