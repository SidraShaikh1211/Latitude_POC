"""Tests for the per-case evidence digest builder.

The digest is the input to every parallel adjudication so it has to be:
  (a) deterministic — same CaseFacts in, same digest out;
  (b) bounded — no pathological case can blow the prompt;
  (c) representative — fact identifiers + key values present so the agent
      can drill in without re-discovering structure via tools.
"""

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
from datetime import date

from app.pas.bundle_parser import CaseContext, FactCollection
from app.policy.evidence_digest import (
    MAX_ITEMS_PER_SECTION,
    MAX_LINE_CHARS,
    build_evidence_digest,
)
from app.policy.tools import CaseFacts


def _full_case() -> CaseFacts:
    extracted = ExtractedFacts(
        patient=ExtractedPatient(given="Jane", family="Doe", birth_date="1985-06-01", gender="female"),
        conditions=[
            ExtractedCondition(
                icd10_code="M54.16",
                display="Radiculopathy, lumbar region",
                onset_date="2025-11-01",
                citations=[Citation(document_id="d1", page=2, quote="lumbar radiculopathy")],
            ),
        ],
        observations=[
            ExtractedObservation(
                code_display="Pain NRS",
                value_quantity=8.0,
                value_unit="/10",
                effective_date="2026-02-15",
                category="pain-score",
                citations=[Citation(document_id="d1", page=3, quote="Pain rating: 8/10")],
            ),
        ],
        medications=[
            ExtractedMedication(
                resource_type="MedicationStatement",
                medication_name="ibuprofen",
                dose="600mg",
                frequency="TID",
                start_date="2025-11-01",
                citations=[Citation(document_id="d1", page=4, quote="ibuprofen 600 mg")],
            ),
        ],
        procedures=[
            ExtractedProcedure(
                cpt_code="97110",
                display="Therapeutic exercise",
                performed_date="2026-01-10",
                citations=[Citation(document_id="d1", page=5, quote="PT therapeutic exercise")],
            ),
        ],
        allergies=[
            ExtractedAllergy(
                substance="penicillin",
                reaction="rash",
                criticality="low",
                citations=[Citation(document_id="d1", page=6, quote="allergy: penicillin")],
            ),
        ],
        diagnostic_reports=[
            ExtractedDiagnosticReport(
                modality="MRI",
                body_site="lumbar spine",
                findings="L5 disc protrusion compressing right L5 nerve root",
                performed_date="2026-01-20",
                citations=[Citation(document_id="d1", page=7, quote="L5 disc protrusion")],
            ),
        ],
    )
    return CaseFacts(bundle_facts=FactCollection(patient={"birthDate": "1985-06-01"}), extracted=extracted)


def test_digest_includes_all_sections():
    d = build_evidence_digest(_full_case())
    for header in (
        "PATIENT:", "CONDITIONS:", "OBSERVATIONS:", "MEDICATIONS:",
        "PROCEDURES (prior / completed):", "ALLERGIES:", "DIAGNOSTIC REPORTS:",
    ):
        assert header in d, f"missing section {header}"


def test_digest_includes_identifiers_and_cite_hints():
    d = build_evidence_digest(_full_case())
    assert "M54.16" in d              # icd10 code is a drilldown handle
    assert "Pain NRS=8.0/10" in d     # observation value summary
    assert "ibuprofen" in d           # medication name
    assert "cite: d1 p.3" in d        # cite hint lets the agent fetch the verbatim quote


def test_digest_is_deterministic():
    case = _full_case()
    assert build_evidence_digest(case) == build_evidence_digest(case)


def test_digest_caps_section_size():
    """A pathologically large case must not blow the prompt."""
    extracted = ExtractedFacts(
        conditions=[
            ExtractedCondition(icd10_code=f"X{i:02d}", display=f"cond {i}")
            for i in range(MAX_ITEMS_PER_SECTION * 3)
        ]
    )
    case = CaseFacts(bundle_facts=FactCollection(), extracted=extracted)
    d = build_evidence_digest(case)
    cond_block = d.split("CONDITIONS:\n", 1)[1]
    assert cond_block.count("\n- ") <= MAX_ITEMS_PER_SECTION


def test_digest_line_truncation():
    """A single fact with absurd display text must be truncated, not dropped."""
    extracted = ExtractedFacts(
        conditions=[ExtractedCondition(icd10_code="Z00", display="x" * (MAX_LINE_CHARS * 2))]
    )
    case = CaseFacts(bundle_facts=FactCollection(), extracted=extracted)
    d = build_evidence_digest(case)
    for line in d.splitlines():
        assert len(line) <= MAX_LINE_CHARS


def test_digest_empty_case_still_renders_safely():
    case = CaseFacts(bundle_facts=FactCollection())
    d = build_evidence_digest(case)
    assert d.startswith("CASE EVIDENCE DIGEST")


# ---------------------------------------------------------------------------
# REQUESTED SERVICE block (only rendered when CaseContext is supplied)
# ---------------------------------------------------------------------------


def _ctx(**overrides) -> CaseContext:
    defaults = dict(
        cpt_code="58571",
        icd10_codes=["N80.03", "N94.6"],
        payer_id="molina",
        line_of_business="medicaid",
        state="NY",
        patient_age=42,
        service_date=date(2026, 4, 8),
        urgency="standard",
        care_setting="outpatient",
        request_category="surgical",
        requested_indication_icd10_codes=["N80.03", "N94.6"],
        cpt_display="Laparoscopic total hysterectomy with tubes/ovaries",
        indication_displays={
            "N80.03": "Adenomyosis of uterus",
            "N94.6": "Dysmenorrhea",
        },
        body_site="pelvis",
    )
    defaults.update(overrides)
    return CaseContext(**defaults)


def test_no_context_keeps_existing_digest_shape():
    """Backwards-compat: omitting context yields a digest that does NOT
    contain the REQUESTED SERVICE block — single-leaf test paths and any
    legacy caller keep working unchanged."""
    d = build_evidence_digest(_full_case())
    assert "REQUESTED SERVICE" not in d


def test_requested_service_block_renders_first():
    """When context is supplied, REQUESTED SERVICE sits at the very top of
    the digest body — before DOCUMENTS or any clinical section."""
    d = build_evidence_digest(_full_case(), context=_ctx(), branch="initial")
    body = d.split("\n", 1)[1]  # drop the boilerplate header line
    assert body.lstrip().startswith("REQUESTED SERVICE:")
    # And the requested-service block precedes the first clinical section.
    assert d.index("REQUESTED SERVICE:") < d.index("PATIENT:")


def test_requested_service_block_populates_all_fields():
    d = build_evidence_digest(_full_case(), context=_ctx(), branch="initial")
    assert "- CPT: 58571 — Laparoscopic total hysterectomy with tubes/ovaries" in d
    assert "- Primary indication: N80.03 — Adenomyosis of uterus" in d
    assert "- Secondary indications: N94.6 — Dysmenorrhea" in d
    assert "- Service date: 2026-04-08" in d
    assert "- Body site: pelvis" in d
    assert "- Urgency: standard" in d
    assert "- Care setting: outpatient" in d
    assert "- Branch: initial" in d


def test_secondary_indications_line_omitted_when_only_primary():
    ctx = _ctx(
        icd10_codes=["N80.03"],
        requested_indication_icd10_codes=["N80.03"],
        indication_displays={"N80.03": "Adenomyosis of uterus"},
    )
    d = build_evidence_digest(_full_case(), context=ctx)
    assert "Primary indication: N80.03 — Adenomyosis of uterus" in d
    assert "Secondary indications:" not in d


def test_body_site_line_omitted_when_empty():
    d = build_evidence_digest(_full_case(), context=_ctx(body_site=""))
    assert "Body site:" not in d


def test_branch_line_omitted_when_branch_is_none():
    d = build_evidence_digest(_full_case(), context=_ctx(), branch=None)
    assert "Branch:" not in d


def test_indication_falls_back_to_bare_code_when_display_missing():
    """A requested ICD without a display entry renders as the bare code, not
    'CODE — '."""
    ctx = _ctx(
        requested_indication_icd10_codes=["N80.03", "N94.6"],
        indication_displays={"N80.03": "Adenomyosis of uterus"},  # N94.6 missing
    )
    d = build_evidence_digest(_full_case(), context=ctx)
    assert "Secondary indications: N94.6\n" in d or "Secondary indications: N94.6" in d
    # Make sure we didn't produce a dangling " — "
    assert "N94.6 — " not in d


def test_cpt_display_falls_back_to_bare_code():
    ctx = _ctx(cpt_display="")
    d = build_evidence_digest(_full_case(), context=ctx)
    assert "- CPT: 58571\n" in d  # no " — " suffix
    assert "- CPT: 58571 — " not in d
