import json

from app.determination.decider import Determination
from app.determination.reviewer import EvidenceCitation, MissingInfo, ReviewerOutput
from app.pas.bundle_builder import build_pas_response_bundle
from app.pas.bundle_parser import parse_pas_bundle
from app.policy.adjudicator import CriterionVerdict
from app.policy.registry import PolicyRegistry, reset_registry
from app.settings import PROJECT_ROOT


def _smith_parsed():
    reset_registry()
    bundle = json.loads((PROJECT_ROOT / "tests/fixtures/smith_claim_bundle.json").read_text())
    return parse_pas_bundle(bundle)


def _registry():
    r = PolicyRegistry()
    r.load_dir()
    return r


def test_builds_pend_bundle_for_smith():
    parsed = _smith_parsed()
    policy = _registry().get("molina-mcp-032")
    determination = Determination(
        outcome="pend",
        rationale="Conservative therapy incomplete + exclusion overlap.",
        triggered_exclusions=[],
        escalation_reasons=[],
    )
    leaf_verdicts = {
        "indication.initial_injection.severity.nrs_above_4": CriterionVerdict(
            criterion_id="indication.initial_injection.severity.nrs_above_4",
            verdict="met", confidence=0.95, reasoning="NRS 9/10 documented.",
        ),
        "indication.initial_injection.conservative_therapy.failure.pt.duration_4_weeks": CriterionVerdict(
            criterion_id="indication.initial_injection.conservative_therapy.failure.pt.duration_4_weeks",
            verdict="unclear", confidence=0.55,
            reasoning="PT planned but documented as too painful to start.",
            missing_info=["Confirm completion of PT for >=4 weeks."],
        ),
    }
    exclusion_verdicts = {
        "X2": CriterionVerdict(
            criterion_id="X2", verdict="unclear", confidence=0.6,
            reasoning="M79.18 appears alongside M54.16; primary indication needs clarification.",
        ),
    }
    reviewer = ReviewerOutput(
        narrative=(
            "Eligibility met (age 50, lumbar radiculopathy M54.16). Severity met "
            "(NRS 9/10). However, conservative therapy is incomplete and primary "
            "indication may overlap with M79.18 exclusion. Pending the case for "
            "two specific data points."
        ),
        missing_info=[
            MissingInfo(
                id="MI1",
                criterion_id="indication.initial_injection.conservative_therapy",
                request=("Document either completion of PT for >=4 weeks at 3-4 "
                         "sessions/week, or imaging correlation and rationale for "
                         "why PT is contraindicated."),
            ),
            MissingInfo(
                id="MI2", criterion_id="exclusion:X2",
                request="Confirm primary indication is lumbar radicular pain (M54.16) and not myofascial pain (M79.18).",
            ),
        ],
    )

    res = build_pas_response_bundle(
        parsed_in=parsed,
        policy=policy,
        determination=determination,
        leaf_verdicts=leaf_verdicts,
        exclusion_verdicts=exclusion_verdicts,
        reviewer=reviewer,
        case_id="smith-case-001",
    )

    assert res.bundle["resourceType"] == "Bundle"
    cr = res.bundle["entry"][0]["resource"]
    assert cr["resourceType"] == "ClaimResponse"
    assert cr["use"] == "preauthorization"
    assert cr["outcome"] == "queued"   # pend → queued in PAS valueset
    assert cr.get("preAuthRef") is None
    # disposition carries the narrative
    assert "Eligibility met" in cr["disposition"]
    # processNote count: 2 leaves + 1 unclear exclusion + 2 missing_info = 5
    assert len(cr["processNote"]) == 5
    # Both info requests appear with MISSING_INFO tag
    notes_text = " ".join(n["text"] for n in cr["processNote"])
    assert "MI1" in notes_text and "MI2" in notes_text


def test_builds_approve_bundle_with_preauthref():
    parsed = _smith_parsed()
    policy = _registry().get("molina-mcp-032")
    determination = Determination(
        outcome="approve",
        rationale="All criteria satisfied.",
        triggered_exclusions=[],
        escalation_reasons=[],
    )
    leaf_verdicts = {
        "indication.initial_injection.severity.nrs_above_4": CriterionVerdict(
            criterion_id="indication.initial_injection.severity.nrs_above_4",
            verdict="met", confidence=0.95, reasoning="NRS 9/10.",
        ),
    }
    reviewer = ReviewerOutput(
        narrative=(
            "All medical necessity criteria satisfied. No exclusions fired. "
            "Approving the requested service."
        ),
    )

    res = build_pas_response_bundle(
        parsed_in=parsed,
        policy=policy,
        determination=determination,
        leaf_verdicts=leaf_verdicts,
        exclusion_verdicts={},
        reviewer=reviewer,
        case_id="approve-case-001",
    )
    cr = res.bundle["entry"][0]["resource"]
    assert cr["outcome"] == "complete"
    assert cr["preAuthRef"] == "approve-case-001"


def test_builds_deny_bundle_when_exclusion_fires():
    parsed = _smith_parsed()
    policy = _registry().get("molina-mcp-032")
    determination = Determination(
        outcome="deny",
        rationale="Exclusion X2 (myofascial pain) fired.",
        triggered_exclusions=["X2"],
        escalation_reasons=[],
    )
    exclusion_verdicts = {
        "X2": CriterionVerdict(
            criterion_id="X2", verdict="met", confidence=0.9,
            reasoning="Primary indication is M79.18.",
        ),
    }
    reviewer = ReviewerOutput(
        narrative=(
            "The primary indication on the submission is M79.18 (other myalgia), "
            "which is excluded from coverage per Molina Policy 032 (Limitations "
            "and Exclusions). Denying the requested service."
        ),
    )
    res = build_pas_response_bundle(
        parsed_in=parsed,
        policy=policy,
        determination=determination,
        leaf_verdicts={},
        exclusion_verdicts=exclusion_verdicts,
        reviewer=reviewer,
        case_id="deny-case-001",
    )
    cr = res.bundle["entry"][0]["resource"]
    assert cr["outcome"] == "error"
    assert cr.get("preAuthRef") is None
    # The exclusion shows up in processNote
    notes_text = " ".join(n["text"] for n in cr["processNote"])
    assert "X2" in notes_text
