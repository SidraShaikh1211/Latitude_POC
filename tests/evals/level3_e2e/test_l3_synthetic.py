"""Level 3 — End-to-end synthetic cases.

For each L3 case in `synthetic_cases/`: build a PAS Bundle wrapping a
synthetic narrative as a PDF, run the orchestrator end-to-end, assert
the expected outcome (approve/deny/pend) and — if pend — that the
reviewer narrative + missing_info mention the expected keywords.

Expensive (~$0.50/case). Gated behind RUN_LLM_EVALS=1.

Add new cases by dropping a module into `synthetic_cases/` that exports
a `CASE: L3SyntheticCase`. The discovery below picks them up
automatically.
"""

import importlib
import os
import pkgutil

import pytest

from app.orchestrator import evaluate_pa_case
from tests.evals.level3_e2e.synthetic_bundle import L3SyntheticCase, build_pas_bundle


_RUN_LLM = pytest.mark.skipif(
    os.environ.get("RUN_LLM_EVALS") != "1",
    reason="Set RUN_LLM_EVALS=1 to run L3 evals (each case ~$0.50 in API).",
)


def _discover_cases() -> list[L3SyntheticCase]:
    """Auto-discover every CASE exported from synthetic_cases/*.py."""
    import tests.evals.level3_e2e.synthetic_cases as pkg
    cases: list[L3SyntheticCase] = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{pkg.__name__}.{info.name}")
        case = getattr(mod, "CASE", None)
        if isinstance(case, L3SyntheticCase):
            cases.append(case)
    return cases


L3_SYNTHETIC_CASES = _discover_cases()


@_RUN_LLM
@pytest.mark.parametrize("case", L3_SYNTHETIC_CASES, ids=lambda c: c.id)
async def test_l3_synthetic(case: L3SyntheticCase):
    bundle = build_pas_bundle(case)
    run = await evaluate_pa_case(
        bundle, run_intake_on_documents=True, case_id=case.id,
    )

    # Selection sanity — must have picked a real policy for approve/deny/pend.
    assert run.selection is not None, f"{case.id}: no selection result"
    if case.expected_outcome != "needs_human_review":
        assert run.selection.status == "ok", (
            f"{case.id}: expected policy selection but selector status={run.selection.status}"
        )

    # Determination outcome
    assert run.determination is not None, f"{case.id}: no determination"
    actual = run.determination.outcome
    print(
        f"\n  {case.id} domain={case.domain} expected={case.expected_outcome} "
        f"actual={actual} policy={run.selection.selected_policy_id}"
    )
    assert actual == case.expected_outcome, (
        f"{case.id}: expected outcome {case.expected_outcome!r}, got {actual!r}. "
        f"Rationale: {run.determination.rationale[:240]}"
    )

    # Reviewer / missing-info checks for pend outcomes
    if case.expected_outcome == "pend" and case.expected_info_keywords:
        assert run.reviewer is not None, f"{case.id}: pend outcome but no reviewer output"
        text = " ".join(mi.request.lower() for mi in run.reviewer.output.missing_info)
        text += " " + run.reviewer.output.narrative.lower()
        missing_kw = [kw for kw in case.expected_info_keywords if kw.lower() not in text]
        assert not missing_kw, (
            f"{case.id}: pend outcome but missing-info text didn't reference: "
            f"{missing_kw}. Got: {text[:300]}"
        )

    # Deny-outcome rationale checks
    if case.expected_outcome == "deny" and case.expected_deny_keywords:
        text = run.determination.rationale.lower()
        if run.reviewer is not None:
            text += " " + run.reviewer.output.narrative.lower()
        missing_kw = [kw for kw in case.expected_deny_keywords if kw.lower() not in text]
        assert not missing_kw, (
            f"{case.id}: deny outcome but rationale didn't reference: "
            f"{missing_kw}. Got: {text[:300]}"
        )


def test_l3_synthetic_summary():
    """Print a summary of L3 cases by domain. Always runs (no LLM cost)."""
    from collections import Counter
    by_domain = Counter(c.domain for c in L3_SYNTHETIC_CASES)
    by_outcome = Counter(c.expected_outcome for c in L3_SYNTHETIC_CASES)
    print(f"\nL3 synthetic cases: {len(L3_SYNTHETIC_CASES)}")
    print("  by domain:")
    for k, v in sorted(by_domain.items()):
        print(f"    {k}: {v}")
    print("  by expected outcome:")
    for k, v in sorted(by_outcome.items()):
        print(f"    {k}: {v}")
