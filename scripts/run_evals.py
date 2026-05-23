"""Run the full 5-level eval pyramid with a metrics report.

Usage:
    .venv/bin/python -m scripts.run_evals               # L0 + L1 + L4 only (no LLM cost)
    RUN_LLM_EVALS=1 .venv/bin/python -m scripts.run_evals   # all 5 levels (LLM cost ~$3-10)
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
    last = result.stdout.strip().split("\n")[-1] if result.stdout else ""
    return {
        "summary": last,
        "rc": result.returncode,
    }


# ---------------------------------------------------------------------------
# Level 2 — Criterion eval (LLM)
# ---------------------------------------------------------------------------

async def run_level2() -> dict:
    from collections import defaultdict
    from app.policy.adjudicator import adjudicate_criterion
    from tests.evals.level2_criteria.cases import L2_CASES, make_case_facts

    reset_registry()
    reg = PolicyRegistry()
    reg.load_dir()

    def _find(policy_id: str, cid: str):
        p = reg.get(policy_id)
        if p is None:
            return None
        for leaf in iter_leaves(p.criteria):
            if leaf.id == cid:
                return leaf
        for ex in p.exclusions:
            if ex.id == cid:
                return ex
        return None

    passes = 0
    failures = []
    total_cost = 0.0
    by_policy: dict[str, dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0})
    for case in L2_CASES:
        criterion = _find(case.policy_id, case.criterion_id)
        if not criterion:
            failures.append((case.id, f"criterion {case.criterion_id!r} not found in {case.policy_id!r}"))
            by_policy[case.policy_id]["fail"] += 1
            continue
        case_facts = make_case_facts(case.facts)
        try:
            result = await adjudicate_criterion(criterion, case_facts)
            total_cost += result.usage.cost_usd
            actual = result.verdict.verdict
            if actual == case.expected_verdict:
                passes += 1
                by_policy[case.policy_id]["pass"] += 1
                print(f"  ✓ {case.id:12s} [{case.policy_id[:14]:14s}] {actual}")
            else:
                failures.append((case.id, f"expected {case.expected_verdict} got {actual}"))
                by_policy[case.policy_id]["fail"] += 1
                print(f"  ✗ {case.id:12s} [{case.policy_id[:14]:14s}] expected {case.expected_verdict} got {actual}")
        except Exception as e:
            failures.append((case.id, str(e)))
            by_policy[case.policy_id]["fail"] += 1
            print(f"  ! {case.id}: {e}")
    return {
        "total": len(L2_CASES),
        "passed": passes,
        "failed": len(failures),
        "pass_rate": passes / len(L2_CASES) if L2_CASES else 0.0,
        "cost_usd": total_cost,
        "failures": failures,
        "by_policy": {k: dict(v) for k, v in by_policy.items()},
    }


# ---------------------------------------------------------------------------
# Level 3 — E2E (LLM)
# ---------------------------------------------------------------------------

async def run_level3() -> dict:
    """Run the Smith keystone (via real PDF→extractor→bundle path) PLUS the
    synthetic L3 cases (3 domains).

    Smith goes through metadata extraction at runtime (no pre-baked fixture)
    so the eval exercises the same path the doctor-submit endpoint does.
    Synthetic cases skip the extractor — their bundles are built directly
    from L3SyntheticCase specs and the orchestrator runs intake-on-documents
    against synthetic narrative PDFs.
    """
    from collections import defaultdict

    from app.extraction.metadata import extract_submission_metadata
    from app.extraction.pdf import extract_pdf
    from app.orchestrator import evaluate_pa_case
    from app.pas.bundle_constructor import build_pas_bundle as build_real_bundle
    from tests.evals.level3_e2e.synthetic_bundle import build_pas_bundle as build_synthetic_bundle
    from tests.evals.level3_e2e.test_l3_synthetic import L3_SYNTHETIC_CASES

    # Smith keystone — real PDF on disk, runs through extractor
    smith_cases = [
        {
            "id": "L3-E01-smith",
            "pdf": "clinical_pdfs/David_Smith_Clinical.pdf",
            "expected_outcome": "pend",
            "expected_policy": "molina-mcp-032",
            "domain": "esi",
        },
    ]

    passes = 0
    failures = []
    total_cost = 0.0
    by_domain: dict[str, dict[str, int]] = defaultdict(lambda: {"pass": 0, "fail": 0})

    # First: Smith via real PDF→extractor→bundle
    for c in smith_cases:
        pdf_path = PROJECT_ROOT / c["pdf"]
        if not pdf_path.exists():
            failures.append((c["id"], f"pdf missing: {c['pdf']}"))
            by_domain[c["domain"]]["fail"] += 1
            continue
        try:
            doc = extract_pdf(pdf_path, document_id=f"L3-{c['id']}-doc")
            pdf_bytes = pdf_path.read_bytes()
            meta = await extract_submission_metadata(
                doc, pdf_bytes=pdf_bytes, pdf_filename=pdf_path.name,
            )
            total_cost += meta.usage.cost_usd
            bundle, _ = build_real_bundle(meta.submission, case_id=f"L3-{c['id']}")
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
                by_domain[c["domain"]]["pass"] += 1
                print(f"  ✓ {c['id']:24s} [{c['domain']:12s}] outcome={actual} policy={run.selection.selected_policy_id}")
            else:
                failures.append((c["id"], f"expected {c['expected_outcome']} got {actual}; policy={run.selection.selected_policy_id}"))
                by_domain[c["domain"]]["fail"] += 1
                print(f"  ✗ {c['id']:24s} [{c['domain']:12s}] expected {c['expected_outcome']} got {actual} (policy={run.selection.selected_policy_id})")
        except Exception as e:
            failures.append((c["id"], str(e)))
            by_domain[c["domain"]]["fail"] += 1
            print(f"  ! {c['id']}: {e}")

    # Then: synthetic L3 cases (no extractor, narrative PDF wrapped inline)
    for syn in L3_SYNTHETIC_CASES:
        bundle = build_synthetic_bundle(syn)
        try:
            run = await evaluate_pa_case(bundle, run_intake_on_documents=True, case_id=syn.id)
            if run.adjudication:
                total_cost += run.adjudication.total_usage.cost_usd
            if run.intake:
                total_cost += run.intake.usage.cost_usd
            actual = run.determination.outcome if run.determination else None
            if actual == syn.expected_outcome:
                passes += 1
                by_domain[syn.domain]["pass"] += 1
                print(f"  ✓ {syn.id:24s} [{syn.domain:12s}] outcome={actual}")
            else:
                failures.append((syn.id, f"expected {syn.expected_outcome} got {actual}"))
                by_domain[syn.domain]["fail"] += 1
                print(f"  ✗ {syn.id:24s} [{syn.domain:12s}] expected {syn.expected_outcome} got {actual}")
        except Exception as e:
            failures.append((syn.id, str(e)))
            by_domain[syn.domain]["fail"] += 1
            print(f"  ! {syn.id}: {e}")

    total = len(smith_cases) + len(L3_SYNTHETIC_CASES)
    return {
        "total": total,
        "passed": passes,
        "failed": len(failures),
        "pass_rate": passes / total if total else 0.0,
        "cost_usd": total_cost,
        "failures": failures,
        "by_domain": {k: dict(v) for k, v in by_domain.items()},
    }


# ---------------------------------------------------------------------------
# Level 4 — Cross-policy selector boundary (deterministic, no LLM)
# ---------------------------------------------------------------------------

def run_level4() -> dict:
    """Run the L4 cross-policy selector tests via pytest."""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/evals/level4_selector_cross_policy/", "-q", "--tb=no"],
        capture_output=True, text=True, cwd=PROJECT_ROOT,
    )
    last = result.stdout.strip().split("\n")[-1] if result.stdout else ""
    return {
        "summary": last,
        "rc": result.returncode,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> int:
    import json, datetime
    run_llm = os.environ.get("RUN_LLM_EVALS") == "1"
    run_id = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")

    results: dict = {"run_id": run_id, "run_llm": run_llm}

    _hr("Level 0 — Citation faithfulness")
    l0 = run_level0()
    results["l0"] = l0
    print(f"  {l0['passed']}/{l0['total']} citations verified ({l0['pass_rate']:.1%})")

    _hr("Level 1 — Policy selector (deterministic)")
    l1 = run_level1()
    results["l1"] = l1
    print(f"  {l1['summary']}")

    _hr("Level 4 — Cross-policy selector boundary (deterministic)")
    l4 = run_level4()
    results["l4"] = l4
    print(f"  {l4['summary']}")

    l2 = l3 = None
    if run_llm:
        _hr("Level 2 — Per-criterion adjudication (LLM)")
        l2 = await run_level2()
        results["l2"] = l2
        print(f"\n  {l2['passed']}/{l2['total']} passed ({l2['pass_rate']:.1%})  cost=${l2['cost_usd']:.4f}")
        if l2.get("by_policy"):
            print("  By policy:")
            for pid, counts in l2["by_policy"].items():
                tot = counts["pass"] + counts["fail"]
                print(f"    {pid:42s}  {counts['pass']}/{tot}")

        _hr("Level 3 — End-to-end determination (LLM)")
        l3 = await run_level3()
        results["l3"] = l3
        print(f"\n  {l3['passed']}/{l3['total']} passed ({l3['pass_rate']:.1%})  cost=${l3['cost_usd']:.4f}")
        if l3.get("by_domain"):
            print("  By domain:")
            for d, counts in l3["by_domain"].items():
                tot = counts["pass"] + counts["fail"]
                print(f"    {d:12s}  {counts['pass']}/{tot}")
    else:
        _hr("Level 2 + Level 3 (LLM-dependent)")
        print("  Skipped. Set RUN_LLM_EVALS=1 to enable (LLM cost ~$2-10).")

    _hr("Summary")
    print(f"  L0 citation:    {l0['pass_rate']:.1%}  (target 100%)")
    print(f"  L1 selector:    {'PASS' if l1['rc'] == 0 else 'FAIL'}  (target ≥98%)")
    print(f"  L4 x-policy:    {'PASS' if l4['rc'] == 0 else 'FAIL'}  (target 100%)")
    if l2:
        print(f"  L2 criterion:   {l2['pass_rate']:.1%}  (target ≥92% clear cases)")
    if l3:
        print(f"  L3 e2e:         {l3['pass_rate']:.1%}  (target ≥85% exact match)")
    if l2 or l3:
        total_cost = (l2['cost_usd'] if l2 else 0) + (l3['cost_usd'] if l3 else 0)
        print(f"  Total LLM cost: ${total_cost:.4f}")

    # Persist results to disk for diff-across-runs
    results_dir = PROJECT_ROOT / "tests" / "evals" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = results_dir / f"{run_id}.json"
    out_path.write_text(json.dumps(results, indent=2, default=str))
    print(f"\nResults written: {out_path.relative_to(PROJECT_ROOT)}")

    failures = (l1.get("rc") != 0) or (l4.get("rc") != 0) or \
               (l2 and l2["failed"] > 0) or (l3 and l3["failed"] > 0)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
