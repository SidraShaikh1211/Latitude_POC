"""Adjudicator tool unit tests."""

from app.extraction.intake import (
    Citation,
    ExtractedCondition,
    ExtractedFacts,
    ExtractedMedication,
    ExtractedObservation,
    ExtractedProcedure,
)
from app.extraction.pdf import extract_pdf
from app.pas.bundle_parser import FactCollection
from app.policy.tools import (
    CaseFacts,
    check_temporal_constraint,
    dispatch_tool,
    get_document_excerpt,
    lookup_term_class_tool,
    request_human_review,
    search_facts_by_type,
    tool_definitions,
)
from app.settings import PROJECT_ROOT


def _make_case() -> CaseFacts:
    extracted = ExtractedFacts(
        conditions=[
            ExtractedCondition(icd10_code="M54.16", display="Radiculopathy, lumbar region",
                               citations=[Citation(document_id="d1", page=1, quote="stub-quote")]),
            ExtractedCondition(icd10_code="M79.18", display="Other myalgia",
                               citations=[Citation(document_id="d1", page=2, quote="stub-quote")]),
        ],
        observations=[
            ExtractedObservation(code_display="Pain NRS", value_quantity=9.0, value_unit="/10",
                                 effective_date="2026-02-15", category="pain-score",
                                 citations=[Citation(document_id="d1", page=3, quote="stub-quote")]),
        ],
        medications=[
            ExtractedMedication(resource_type="MedicationStatement", medication_name="ibuprofen",
                                start_date="2025-11-01",
                                citations=[Citation(document_id="d1", page=4, quote="stub-quote")]),
            ExtractedMedication(resource_type="MedicationStatement", medication_name="acetaminophen",
                                start_date="2025-11-01",
                                citations=[Citation(document_id="d1", page=4, quote="stub-quote")]),
        ],
        procedures=[
            ExtractedProcedure(cpt_code="97110", display="PT therapeutic exercise",
                               performed_date="2026-01-10",
                               citations=[Citation(document_id="d1", page=5, quote="stub-quote")]),
        ],
    )
    return CaseFacts(bundle_facts=FactCollection(), extracted=extracted)


# ---------------------------------------------------------------------------
# search_facts_by_type
# ---------------------------------------------------------------------------

def test_search_returns_conditions():
    case = _make_case()
    items = search_facts_by_type(case, "Condition")
    icds = {it.get("icd10_code") for it in items}
    assert icds == {"M54.16", "M79.18"}


def test_search_filters_by_icd10_pattern():
    case = _make_case()
    items = search_facts_by_type(case, "Condition", filters={"icd10_pattern": "M54.*"})
    assert len(items) == 1
    assert items[0]["icd10_code"] == "M54.16"


def test_search_filters_by_icd10_class():
    case = _make_case()
    items = search_facts_by_type(case, "Condition", filters={"icd10_class": "myofascial_pain"})
    assert len(items) == 1
    assert items[0]["icd10_code"] == "M79.18"


def test_search_filters_by_drug_class():
    case = _make_case()
    items = search_facts_by_type(case, "MedicationStatement", filters={"drug_class": "NSAID"})
    names = {it.get("medication_name") for it in items}
    assert "ibuprofen" in names
    assert "acetaminophen" not in names


def test_search_unknown_type_returns_error():
    case = _make_case()
    items = search_facts_by_type(case, "BogusType")
    assert items and items[0].get("error")


# ---------------------------------------------------------------------------
# get_document_excerpt
# ---------------------------------------------------------------------------

def test_document_excerpt_smith_page_1():
    smith = extract_pdf(PROJECT_ROOT / "clinical_pdfs" / "David_Smith_Clinical.pdf",
                        document_id="smith")
    case = CaseFacts(bundle_facts=FactCollection(), documents={"smith": smith})
    out = get_document_excerpt(case, "smith", 1)
    assert "excerpt" in out
    assert len(out["excerpt"]) > 100


def test_document_excerpt_unknown_doc():
    case = CaseFacts(bundle_facts=FactCollection())
    out = get_document_excerpt(case, "missing", 1)
    assert "error" in out


def test_document_excerpt_out_of_range_page():
    smith = extract_pdf(PROJECT_ROOT / "clinical_pdfs" / "David_Smith_Clinical.pdf",
                        document_id="smith")
    case = CaseFacts(bundle_facts=FactCollection(), documents={"smith": smith})
    out = get_document_excerpt(case, "smith", 999)
    assert "error" in out


# ---------------------------------------------------------------------------
# check_temporal_constraint
# ---------------------------------------------------------------------------

def test_temporal_min_duration_weeks_pass():
    r = check_temporal_constraint("min_duration_weeks", duration_weeks=4.5, threshold_weeks=4)
    assert r["passes"]


def test_temporal_min_duration_weeks_fail():
    r = check_temporal_constraint("min_duration_weeks", duration_weeks=2.0, threshold_weeks=4)
    assert not r["passes"]


def test_temporal_dates_to_duration():
    r = check_temporal_constraint(
        "min_duration_weeks",
        dates=["2026-01-01", "2026-02-15"],
        threshold_weeks=4,
    )
    assert r["passes"]
    assert abs(r["computed"] - 45 / 7.0) < 0.1


def test_temporal_min_frequency():
    r = check_temporal_constraint(
        "min_frequency_per_week", frequency_per_week=2.0, threshold_frequency=3,
    )
    assert not r["passes"]


def test_temporal_min_count():
    r = check_temporal_constraint("min_count", count=12, threshold_count=12)
    assert r["passes"]
    r = check_temporal_constraint("min_count", count=11, threshold_count=12)
    assert not r["passes"]


def test_temporal_min_days_between():
    r = check_temporal_constraint(
        "min_days_between",
        dates=["2026-01-01", "2026-01-20", "2026-02-15"],
        threshold_days=14,
    )
    assert r["passes"]
    r = check_temporal_constraint(
        "min_days_between",
        dates=["2026-01-01", "2026-01-05"],
        threshold_days=14,
    )
    assert not r["passes"]


def test_temporal_unknown_constraint():
    r = check_temporal_constraint("bogus")
    assert "error" in r


def test_temporal_missing_args():
    r = check_temporal_constraint("min_duration_weeks")
    assert not r["passes"]
    assert "missing" in r["rationale"]


# ---------------------------------------------------------------------------
# lookup_term_class_tool
# ---------------------------------------------------------------------------

def test_term_class_med():
    r = lookup_term_class_tool("ibuprofen", "NSAID")
    assert r["in_class"] is True
    assert r["source"] == "dictionary"


def test_term_class_med_negative():
    r = lookup_term_class_tool("acetaminophen", "NSAID")
    assert r["in_class"] is False


def test_term_class_icd10():
    r = lookup_term_class_tool("M54.16", "lumbar_radiculopathy", domain="icd10")
    assert r["in_class"] is True


def test_term_class_unknown_domain():
    r = lookup_term_class_tool("x", "y", domain="bogus")
    assert "error" in r


# ---------------------------------------------------------------------------
# request_human_review
# ---------------------------------------------------------------------------

def test_request_human_review_appends_escalation():
    case = _make_case()
    out = request_human_review(case, "criterion structurally ambiguous", "C1.x")
    assert out["escalated"]
    assert case.escalations == ["[C1.x] criterion structurally ambiguous"]


# ---------------------------------------------------------------------------
# dispatch_tool (the agent-loop entry point)
# ---------------------------------------------------------------------------

def test_dispatch_routes_search():
    case = _make_case()
    out = dispatch_tool(case, "search_facts_by_type", {"fact_type": "Condition"})
    assert isinstance(out, list)


def test_dispatch_unknown_tool():
    case = _make_case()
    out = dispatch_tool(case, "no_such_tool", {})
    assert out.get("error")


# ---------------------------------------------------------------------------
# Tool definitions schema sanity
# ---------------------------------------------------------------------------

def test_tool_definitions_shape():
    defs = tool_definitions()
    assert len(defs) == 5
    names = {d["name"] for d in defs}
    assert names == {
        "search_facts_by_type", "get_document_excerpt",
        "check_temporal_constraint", "lookup_term_class",
        "request_human_review",
    }
    for d in defs:
        assert "description" in d and len(d["description"]) > 30
        assert "input_schema" in d
        assert d["input_schema"]["type"] == "object"
