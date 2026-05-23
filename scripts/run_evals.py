"""Run the full 4-level eval pyramid with a metrics report.

Usage:
    .venv/bin/python -m scripts.run_evals               # L0 + L1 only (no LLM cost)
    RUN_LLM_EVALS=1 .venv/bin/python -m scripts.run_evals   # L0 + L1 + L2 + L3 (LLM cost ~$2-10)
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from app.policy.registry import PolicyRegistry, iter_leaves, reset_registry


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _hr(title: str) -> None:
    print(f"\n{'=' * 60}\n {title}\n{'=' * 60}")


# ---------------------------------------------------------------------------
# Level 0 — Citation faithfulness
# ---------------------------------------------------------------------------

def run_level0() -> dict:
    from app.llm.citation_verify import verify_substring
    reset_registry()
    reg = PolicyRegistry()
    reg.load_dir()
    cits = []
    for p in reg.all_policies():
        def visit(node, path):
            cit = getattr(node, "policy_citation", None)
            if cit is not None:
                cits.append((p.policy_id, path, cit))
            if hasattr(node, "children"):
                for c in node.children:
                    visit(c, f"{path}>{c.id}")
        visit(p.criteria, p.criteria.id)
        for ex in p.exclusions:
            cits.append((p.policy_id, f"exclusion:{ex.id}", ex.policy_citation))
    failures = 0
    for pid, path, cit in cits:
        p = reg.get(pid)
        if not verify_substring(cit.quote, p.page_text(cit.page)).found:
            failures += 1
    return {
        "total": len(cits),
        "passed": len(cits) - failures,
        "failed": failures,
        "pass_rate": (len(cits) - failures) / len(cits) if cits else 0.0,
    }


# ---------------------------------------------------------------------------
# Level 1 — Selector eval
# ---------------------------------------------------------------------------

def run_level1() -> dict:
    """Run the L1 selector eval by invoking pytest programmatically."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/evals/level1_selector/", "-q", "--tb=no"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    # Parse "16 passed" or "X passed, Y failed" out of pytest output
    last = result.stdout.strip().split("\n")[-1] if result.stdout else ""
    return {
        "summary": last,
        "rc": result.returncode,
    }


# ---------------------------------------------------------------------------
# Level 2 — Criterion eval (LLM)
# ---------------------------------------------------------------------------

async def run_level2() -> dict:
    from app.policy.adjudicator import adjudicate_criterion
    from tests.evals.level2_criteria.cases import L2_CASES, make_case_facts

    reset_registry()
    reg = PolicyRegistry()
    reg.load_dir()
    policy = reg.get("molina-mcp-032")

    def _find(cid):
        for leaf in iter_leaves(policy.criteria):
            if leaf.id == cid:
                return leaf
        for ex in policy.exclusions:
            if ex.id == cid:
                return ex
        return None

    passes = 0
    failures = []
    total_cost = 0.0
    for case in L2_CASES:
        criterion = _find(case.criterion_id)
        if not criterion:
            failures.append((case.id, "criterion not found"))
            continue
        case_facts = make_case_facts(case.facts)
        try:
            result = await adjudicate_criterion(criterion, case_facts)
            total_cost += result.usage.cost_usd
            actual = result.verdict.verdict
            if actual == case.expected_verdict:
                passes += 1
                print(f"  ✓ {case.id} [{case.category}]: {actual}")
            else:
                failures.append((case.id, f"expected {case.expected_verdict} got {actual}"))
                print(f"  ✗ {case.id} [{case.category}]: expected {case.expected_verdict} got {actual}")
        except Exception as e:
            failures.append((case.id, str(e)))
            print(f"  ! {case.id}: {e}")
    return {
        "total": len(L2_CASES),
        "passed": passes,
        "failed": len(failures),
        "pass_rate": passes / len(L2_CASES) if L2_CASES else 0.0,
        "cost_usd": total_cost,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Level 3 — E2E (LLM)
# ---------------------------------------------------------------------------

async def run_level3() -> dict:
    """Run E2E cases via the real PDF→extractor→bundle→pipeline path.

    Each case is a PDF on disk; the metadata extractor pulls submission
    fields at runtime (no pre-baked fixtures)."""
    from app.extraction.metadata import extract_submission_metadata
    from app.extraction.pdf import extract_pdf
    from app.orchestrator import evaluate_pa_case
    from app.pas.bundle_constructor import build_pas_bundle

    cases = [
        {
            "id": "E01-smith",
            "pdf": "clinical_pdfs/David_Smith_Clinical.pdf",
            "expected_outcome": "pend",
            "expected_policy": "molina-mcp-032",
        },
    ]
    passes = 0
    failures = []
    total_cost = 0.0
    for c in cases:
        pdf_path = PROJECT_ROOT / c["pdf"]
        if not pdf_path.exists():
            failures.append((c["id"], f"pdf missing: {c['pdf']}"))
            continue
        try:
            doc = extract_pdf(pdf_path, document_id=f"L3-{c['id']}-doc")
            pdf_bytes = pdf_path.read_bytes()
            # Metadata extraction (LLM)
            meta = await extract_submission_metadata(
                doc, pdf_bytes=pdf_bytes, pdf_filename=pdf_path.name,
            )
            total_cost += meta.usage.cost_usd
            bundle, _ = build_pas_bundle(meta.submission, case_id=f"L3-{c['id']}")
            run = await evaluate_pa_case(
                bundle, run_intake_on_documents=True, case_id=f"L3-{c['id']}",
            )
            if run.adjudication:
                total_cost += run.adjudication.total_usage.cost_usd
            if run.intake:
                total_cost += run.intake.usage.cost_usd
            actual = run.determination.outcome if run.determination else None
            policy_ok = (
                c.get("expected_policy") is None
                or run.selection.selected_policy_id == c["expected_policy"]
            )
            if actual == c["expected_outcome"] and policy_ok:
                passes += 1
                print(f"  ✓ {c['id']}: outcome={actual} policy={run.selection.selected_policy_id} cost=${total_cost:.4f}")
            else:
                failures.append((c["id"], f"expected {c['expected_outcome']} got {actual}; policy={run.selection.selected_policy_id}"))
                print(f"  ✗ {c['id']}: expected {c['expected_outcome']} got {actual} (policy={run.selection.selected_policy_id})")
        except Exception as e:
            failures.append((c["id"], str(e)))
            print(f"  ! {c['id']}: {e}")
    return {
        "total": len(cases),
        "passed": passes,
        "failed": len(failures),
        "pass_rate": passes / len(cases) if cases else 0.0,
        "cost_usd": total_cost,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    run_llm = os.environ.get("RUN_LLM_EVALS") == "1"

    _hr("Level 0 — Citation faithfulness")
    l0 = run_level0()
    print(f"  {l0['passed']}/{l0['total']} citations verified ({l0['pass_rate']:.1%})")

    _hr("Level 1 — Policy selector (15 cases)")
    l1 = run_level1()
    print(f"  {l1['summary']}")

    l2 = l3 = None
    if run_llm:
        _hr("Level 2 — Per-criterion adjudication")
        l2 = await run_level2()
        print(f"\n  {l2['passed']}/{l2['total']} passed ({l2['pass_rate']:.1%})  cost=${l2['cost_usd']:.4f}")

        _hr("Level 3 — End-to-end determination")
        l3 = await run_level3()
        print(f"\n  {l3['passed']}/{l3['total']} passed ({l3['pass_rate']:.1%})  cost=${l3['cost_usd']:.4f}")
    else:
        _hr("Level 2 + Level 3")
        print("  Skipped. Set RUN_LLM_EVALS=1 to enable (LLM cost ~$2-10).")

    _hr("Summary")
    print(f"  L0 citation:  {l0['pass_rate']:.1%}  (target 100%)")
    print(f"  L1 selector:  {'PASS' if l1['rc'] == 0 else 'FAIL'}  (target ≥98%)")
    if l2:
        print(f"  L2 criterion: {l2['pass_rate']:.1%}  (target ≥92% clear cases)")
    if l3:
        print(f"  L3 e2e:       {l3['pass_rate']:.1%}  (target ≥85% exact match)")
    if l2:
        print(f"  Total LLM cost: ${(l2['cost_usd'] + (l3['cost_usd'] if l3 else 0)):.4f}")

    failures = (l1.get("rc") != 0) or (l2 and l2["failed"] > 0) or (l3 and l3["failed"] > 0)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
