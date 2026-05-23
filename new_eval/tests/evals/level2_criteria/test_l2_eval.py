"""Level 2 — Per-criterion adjudication eval.

For each L2Case: build the criterion + facts, call adjudicate_criterion,
compare verdict to expected. Requires ANTHROPIC_API_KEY (calls real LLM).

Skipped by default in CI unless RUN_LLM_EVALS=1 (each case is ~$0.01-0.05).

Run explicitly:
    RUN_LLM_EVALS=1 .venv/bin/pytest tests/evals/level2_criteria/ -v
"""

import os
from collections import defaultdict

import pytest

from app.policy.adjudicator import adjudicate_criterion
from app.policy.registry import PolicyRegistry, iter_leaves, reset_registry
from tests.evals.level2_criteria.cases import L2_CASES, make_case_facts


_RUN_LLM = pytest.mark.skipif(
    os.environ.get("RUN_LLM_EVALS") != "1",
    reason="Set RUN_LLM_EVALS=1 to run LLM-dependent evals (each case ~$0.01-0.05).",
)


@pytest.fixture(scope="module")
def registry():
    reset_registry()
    r = PolicyRegistry()
    r.load_dir()
    return r


def _find_criterion(registry, policy_id, criterion_id):
    policy = registry.get(policy_id)
    if policy is None:
        raise ValueError(f"policy {policy_id!r} not loaded in registry")
    for leaf in iter_leaves(policy.criteria):
        if leaf.id == criterion_id:
            return leaf
    for ex in policy.exclusions:
        if ex.id == criterion_id:
            return ex
    raise ValueError(f"criterion {criterion_id!r} not found in policy {policy_id!r}")


@_RUN_LLM
@pytest.mark.parametrize("case", L2_CASES, ids=lambda c: c.id)
async def test_level2_case(case, registry):
    criterion = _find_criterion(registry, case.policy_id, case.criterion_id)
    case_facts = make_case_facts(case.facts)
    result = await adjudicate_criterion(criterion, case_facts)
    actual = result.verdict.verdict
    print(f"\n  {case.id} [{case.category}] expected={case.expected_verdict} actual={actual} conf={result.verdict.confidence:.2f}")
    assert actual == case.expected_verdict, (
        f"{case.id} [{case.category}]: expected {case.expected_verdict}, "
        f"got {actual} (conf {result.verdict.confidence:.2f}). "
        f"Reasoning: {result.verdict.reasoning[:200]}"
    )


def test_level2_summary():
    """Print a per-category summary of the L2 test suite size."""
    by_cat = defaultdict(int)
    for c in L2_CASES:
        by_cat[c.category] += 1
    print(f"\nL2 eval suite size: {len(L2_CASES)} cases across {len(by_cat)} categories")
    for cat, n in sorted(by_cat.items()):
        print(f"  {cat}: {n}")
