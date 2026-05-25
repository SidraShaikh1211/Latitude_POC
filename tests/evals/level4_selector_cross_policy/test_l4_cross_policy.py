"""Level 4 — Cross-policy selector boundary cases.

Where L1 tests the selector against Molina alone (with synthetic peers for
disambiguation), L4 tests the selector against the REAL registry of three
policies: Molina ESI + MassHealth Zepbound + Oregon adenomyosis. Each case
exercises the selector's ability to pick the right policy when there are
multiple real candidates loaded.

No LLM calls. Runs in milliseconds.
"""

from datetime import date

import pytest

from app.pas.bundle_parser import CaseContext
from app.policy.registry import PolicyRegistry
from app.policy.selector import select_policy


@pytest.fixture(scope="module")
def reg() -> PolicyRegistry:
    r = PolicyRegistry()
    r.load_dir()
    return r


def _ctx(
    *,
    cpt: str,
    icd10: list[str],
    payer: str,
    lob: str = "medicaid",
    state: str,
    age: int = 45,
    service_date: str = "2026-04-08",
    setting: str = "outpatient",
    category: str = "procedural",
    requested_icd10: list[str] | None = None,
) -> CaseContext:
    return CaseContext(
        cpt_code=cpt,
        icd10_codes=icd10,
        payer_id=payer,
        line_of_business=lob,
        state=state,
        patient_age=age,
        service_date=date.fromisoformat(service_date),
        care_setting=setting,
        request_category=category,
        requested_indication_icd10_codes=(
            requested_icd10 if requested_icd10 is not None else list(icd10)
        ),
    )


# ---------------------------------------------------------------------------
# Happy-path cases — each policy correctly selected with no cross-pollution
# ---------------------------------------------------------------------------


def test_L4_SEL_01_esi_goes_to_molina(reg):
    """ESI request lands on molina-mcp-032, NOT on the other two policies."""
    res = select_policy(
        _ctx(cpt="62323", icd10=["M54.16"], payer="molina", state="NY", age=50),
        registry=reg,
    )
    assert res.status == "ok"
    assert res.selected_policy_id == "molina-mcp-032"


def test_L4_SEL_02_hysterectomy_goes_to_oregon(reg):
    """Adenomyosis hysterectomy lands on oregon-hcr-39, NOT Molina or MassHealth."""
    res = select_policy(
        _ctx(cpt="58570", icd10=["N80.03"], payer="oregon-hca", state="OR", age=46, category="surgical"),
        registry=reg,
    )
    assert res.status == "ok"
    assert res.selected_policy_id == "oregon-hcr-39"


def test_L4_SEL_03_zepbound_goes_to_masshealth(reg):
    """Zepbound request lands on masshealth-anti-obesity-zepbound, NOT Molina or Oregon."""
    res = select_policy(
        _ctx(cpt="tirzepatide-zepbound", icd10=["E66.01"], payer="masshealth", state="MA", age=45, category="pharmacy"),
        registry=reg,
    )
    assert res.status == "ok"
    assert res.selected_policy_id == "masshealth-anti-obesity-zepbound"


# ---------------------------------------------------------------------------
# Cross-policy elimination — wrong-domain requests don't fall through
# ---------------------------------------------------------------------------


def test_L4_SEL_04_hysterectomy_cpt_to_molina_no_match(reg):
    """Hysterectomy CPT submitted with payer=molina — Molina policy is ESI-only,
    so no policy matches. Selector must not silently fall back."""
    res = select_policy(
        _ctx(cpt="58570", icd10=["N80.03"], payer="molina", state="NY", age=46, category="surgical"),
        registry=reg,
    )
    assert res.status == "no_match"
    # selected_policy_id must be None
    assert res.selected_policy_id is None


def test_L4_SEL_05_esi_under_age_18(reg):
    """ESI requested for 16yo — Molina's age_min=18 eliminates the only candidate."""
    res = select_policy(
        _ctx(cpt="62323", icd10=["M54.16"], payer="molina", state="NY", age=16),
        registry=reg,
    )
    assert res.status == "no_match"
    assert any("age" in r.lower() for _, r in res.eliminated)


def test_L4_SEL_06_esi_before_effective_date(reg):
    """ESI requested for DOS 2023-06-01 — before Molina effective_from 2024-08-14."""
    res = select_policy(
        _ctx(cpt="62323", icd10=["M54.16"], payer="molina", state="NY", age=50, service_date="2023-06-01"),
        registry=reg,
    )
    assert res.status == "no_match"
    assert any("effective_from" in r for _, r in res.eliminated)


def test_L4_SEL_07_zepbound_for_minor(reg):
    """Zepbound for 14yo — the Zepbound policy requires age ≥18. With Wegovy
    pediatric branch not modeled in this version, the case has no_match."""
    res = select_policy(
        _ctx(cpt="tirzepatide-zepbound", icd10=["E66.01"], payer="masshealth", state="MA", age=14, category="pharmacy"),
        registry=reg,
    )
    assert res.status == "no_match"
    assert any("age" in r.lower() for _, r in res.eliminated)


def test_L4_SEL_08_endometriosis_goes_to_endometriosis_policy(reg):
    """Endometriosis-only (N80.1) routes to oregon-hcr-39-endometriosis.

    Since the adenomyosis policy's ICD patterns were tightened to literal
    codes (N80.03 only, no N80.* glob), it is now filter-eliminated for any
    non-adenomyosis N80 code. Only the endometriosis policy survives Tier 1,
    so no tiebreaker runs — `tiebreaker_used` is None. This is the policy-
    precision fix: cross-policy contamination is eliminated at the filter
    level, not via a runtime tiebreaker."""
    res = select_policy(
        _ctx(cpt="58570", icd10=["N80.1"], payer="oregon-hca", state="OR", age=42, category="surgical"),
        registry=reg,
    )
    assert res.status == "ok"
    assert res.selected_policy_id == "oregon-hcr-39-endometriosis"
    assert res.tiebreaker_used is None


def test_L4_SEL_09_adenomyosis_still_goes_to_adenomyosis_policy(reg):
    """Regression guard: N80.03 (adenomyosis) routes to oregon-hcr-39.

    The endometriosis policy's icd10_patterns intentionally enumerate
    N80.0/N80.1/.../N80.9 (omitting N80.03), so the filter eliminates it
    before any tier-based ranking. Only oregon-hcr-39 (broad N80.*) survives
    Tier 1, and is picked unambiguously."""
    res = select_policy(
        _ctx(cpt="58570", icd10=["N80.03"], payer="oregon-hca", state="OR", age=46, category="surgical"),
        registry=reg,
    )
    assert res.status == "ok"
    assert res.selected_policy_id == "oregon-hcr-39"


def test_L4_SEL_11_endometriosis_with_unrelated_comorbids(reg):
    """Indication-only discipline + primary weighting: a hysterectomy request
    driven by endometriosis can carry symptom secondaries (dysmenorrhea,
    pelvic pain) that happen to match the *adenomyosis* policy's literal
    codes. The principal endometriosis ICD must still pull selection to the
    endometriosis-specific policy.

    This is the Maria-Santos-style scenario: same CPT, both gyn policies pass
    Phase A (CPT) and Phase B (ICD — endo matches N80.01 literal, adenomyosis
    matches via N94.6 dysmenorrhea), Phase C (context) doesn't distinguish,
    and tier 3 has to resolve the tie. With principal-weighting=2x, the
    literal hit on N80.01 (weight 2) wins over the literal hit on N94.6
    (weight 1)."""
    res = select_policy(
        _ctx(
            cpt="58571",
            icd10=["N80.01", "N80.121", "N80.32", "R10.2", "N94.6"],
            payer="oregon-hca",
            state="OR",
            age=46,
            category="surgical",
        ),
        registry=reg,
    )
    assert res.status == "ok"
    assert res.selected_policy_id == "oregon-hcr-39-endometriosis"


def test_L4_SEL_12_filter_phases_tagged_in_audit(reg):
    """Audit trail tags each elimination with the phase that knocked it out.
    Wrong-CPT policies get `[cpt]`; right-CPT-wrong-ICD get `[icd10]`;
    right-clinical-wrong-context get `[context]`. Useful for the doctor-
    facing UI to explain *why* a policy didn't apply."""
    res = select_policy(
        _ctx(cpt="62323", icd10=["M54.16"], payer="molina", state="NY", age=50),
        registry=reg,
    )
    assert res.status == "ok"
    for _pid, reason in res.eliminated:
        assert reason.startswith(("[cpt]", "[icd10]", "[context]"))


def test_L4_SEL_10_diabetes_med_with_obesity_comorbidity(reg):
    """Patient has both diabetes (E11.9) and obesity (E66.01); the bundle
    declares the requested line item is *for* diabetes only. Two synthetic
    policies cover the same drug — one diabetes-specific, one that covers
    both indications. The diabetes-specific policy must win.

    Demonstrates the design generalizes beyond the Oregon hysterectomy case:
    Tier 1 uses requested_indication_icd10_codes to filter obesity-only
    policies out; Tier 2 pattern-narrowness picks the diabetes-specific
    policy over the broader one if both pass."""
    from app.policy.registry import (
        AppliesTo,
        CriterionLeaf,
        CriterionNode,
        Policy,
        PolicyCitation,
    )

    def _synth(*, policy_id: str, icd10: list[str]) -> Policy:
        cit = PolicyCitation(page=1, section=None, quote="stub")
        leaf = CriterionLeaf(
            id=f"{policy_id}.leaf", type="leaf", description="",
            policy_citation=cit, evaluation={}, verdict_rubric={},
        )
        return Policy(
            policy_id=policy_id, name=policy_id, payer_id="acme",
            version="2026-01-01", effective_from="2026-01-01", effective_until=None,
            source_pdf_path=None, source_total_pages=0,  # type: ignore[arg-type]
            applies_to=AppliesTo(
                cpt_codes=["jardiance-empagliflozin"], hcpcs_codes=[],
                icd10_patterns=icd10,
                lines_of_business=["commercial"], states=["MA"],
                age_min=18, age_max=None,
                settings_of_care=["outpatient"], request_categories=["pharmacy"],
                branches={}, payer_id="acme",
            ),
            criteria=CriterionNode(
                id="root", type="internal", operator="ALL",
                description="", children=[leaf],
            ),
            exclusions=[],
        )

    # Build an isolated registry with just our two synthetic policies so the
    # five-real-policy registry isn't polluted.
    from app.policy.registry import PolicyRegistry
    iso = PolicyRegistry()
    diabetes_only = _synth(policy_id="acme-jardiance-diabetes", icd10=["E11.*"])
    covers_both = _synth(
        policy_id="acme-jardiance-broad",
        icd10=["E11.*", "E66.*"],
    )
    iso._by_id[diabetes_only.policy_id] = diabetes_only
    iso._by_id[covers_both.policy_id] = covers_both
    for p in (diabetes_only, covers_both):
        for c in p.applies_to.cpt_codes:
            iso._by_cpt[c].append(p)
        iso._by_payer[p.payer_id].append(p)

    res = select_policy(
        _ctx(
            cpt="jardiance-empagliflozin",
            icd10=["E11.9", "E66.01"],   # full patient diagnosis list
            requested_icd10=["E11.9"],   # claim.item.diagnosisSequence → diabetes
            payer="acme", lob="commercial", state="MA", age=58, category="pharmacy",
        ),
        registry=iso,
    )
    assert res.status == "ok"
    assert res.selected_policy_id == "acme-jardiance-diabetes"
    assert res.tiebreaker_used == "icd10_pattern_narrowness"
