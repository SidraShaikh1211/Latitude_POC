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


def test_L4_SEL_08_endometriosis_to_oregon_adenomyosis_policy(reg):
    """Endometriosis-only (no adenomyosis) routed to the oregon-hcr-39 policy.
    The policy's applies_to.icd10 includes N80.* (which matches N80.03 adenomyosis
    AND N80.1 endometriosis), so the SELECTOR matches — but Section A criteria
    apply, not B. Selector returns ok; downstream X1 exclusion should fire."""
    res = select_policy(
        _ctx(cpt="58570", icd10=["N80.1"], payer="oregon-hca", state="OR", age=42, category="surgical"),
        registry=reg,
    )
    # Selector matches (the policy's icd10 pattern N80.* is broad)
    assert res.status == "ok"
    assert res.selected_policy_id == "oregon-hcr-39"
    # The downstream adjudicator will fire X1 exclusion — that's tested at L3.
