"""Level 3 — End-to-end determination eval (Smith keystone).

The Smith case must produce `pend` with missing-info requests that include
references to PT completion and/or imaging correlation. This is the demo's
gold-standard test.

Each L3 case is expensive (~$0.50-$2 of API). Gated behind RUN_LLM_EVALS=1.

Additional E2E cases (clean approve, clean deny, sparse) are scaffolded
below as parametrized examples; expand by adding entries.
"""

import json
import os

import pytest

from app.orchestrator import evaluate_pa_case
from app.settings import PROJECT_ROOT


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_EVALS") != "1",
    reason="Set RUN_LLM_EVALS=1 to run end-to-end evals (each case ~$0.50-2 in API).",
)


SMITH_FIXTURE = PROJECT_ROOT / "tests" / "fixtures" / "smith_claim_bundle.json"


async def test_E01_smith_keystone_produces_pend_with_info_requests():
    """E01 — Smith. Expected: pend with ≥1 missing-info request referencing
    PT documentation or imaging correlation."""
    bundle = json.loads(SMITH_FIXTURE.read_text())
    run = await evaluate_pa_case(bundle, run_intake_on_documents=True, case_id="L3-E01-smith")

    # Keystone assertions
    assert run.selection.status == "ok"
    assert run.selection.selected_policy_id == "molina-mcp-032"
    assert run.selection.branch == "initial"
    assert run.determination is not None
    assert run.determination.outcome == "pend", (
        f"Expected Smith → pend; got {run.determination.outcome}. "
        f"Rationale: {run.determination.rationale}"
    )

    assert run.reviewer is not None
    out = run.reviewer.output
    assert len(out.missing_info) >= 1, "Expected at least one missing-info request"

    # At least one info request should mention PT or imaging
    text = " ".join(mi.request.lower() for mi in out.missing_info)
    assert any(kw in text for kw in ("pt", "physical therapy", "imaging", "mri", "ct")), (
        f"Expected info request mentioning PT or imaging; got: {[mi.request for mi in out.missing_info]}"
    )

    # PAS Bundle should be valid (R4) and have outcome=queued (pend → queued)
    assert run.response is not None
    cr = run.response.bundle["entry"][0]["resource"]
    assert cr["resourceType"] == "ClaimResponse"
    assert cr["outcome"] == "queued"

    print(f"\n✓ E01 Smith: outcome={run.determination.outcome}")
    print(f"  missing_info ({len(out.missing_info)}):")
    for mi in out.missing_info:
        print(f"    [{mi.id}] {mi.request[:100]}...")
