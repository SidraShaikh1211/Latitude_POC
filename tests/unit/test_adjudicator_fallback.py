"""Tests for the per-leaf budget + graceful fallback behavior.

Goal: one slow or broken leaf must NOT kill the whole batch. It should be
recorded as `unclear` with a clear missing_info note and the rest of the
batch should complete.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.policy.adjudicator import (
    AdjudicationResult,
    CriterionVerdict,
    _fallback_result,
    adjudicate_all,
)
from app.policy.registry import CriterionLeaf, CriterionNode, Exclusion, PolicyCitation
from app.policy.tools import CaseFacts
from app.pas.bundle_parser import FactCollection


def _leaf(leaf_id: str) -> CriterionLeaf:
    return CriterionLeaf(
        id=leaf_id,
        type="leaf",
        description=f"description for {leaf_id}",
        policy_citation=PolicyCitation(page=1, section="A", quote="stub quote"),
        evaluation={},
        verdict_rubric={"met": "X met", "not_met": "X not met"},
    )


def _met_result(leaf_id: str) -> AdjudicationResult:
    from app.llm.client import Usage
    return AdjudicationResult(
        verdict=CriterionVerdict(
            criterion_id=leaf_id, verdict="met", confidence=0.9, reasoning="ok",
        ),
        iterations=1,
        usage=Usage(),
        tool_calls=[],
    )


def test_fallback_result_shape():
    r = _fallback_result("C1", "leaf budget exceeded")
    assert r.verdict.verdict == "unclear"
    assert r.verdict.confidence == 0.2
    assert "leaf budget exceeded" in r.verdict.reasoning
    assert "leaf budget exceeded" in r.verdict.missing_info[0]
    assert r.iterations == 0


@pytest.mark.asyncio
async def test_one_timeout_does_not_kill_batch():
    """If leaf B hangs past its budget, leaves A and C still get verdicts and
    leaf B is recorded as `unclear` rather than raising."""
    root = CriterionNode(
        id="root", type="internal", operator="ALL", description="",
        children=[_leaf("A"), _leaf("B"), _leaf("C")],
    )

    async def fake_adjudicate(leaf, case, *, digest=None, policy=None):
        if leaf.id == "B":
            await asyncio.sleep(5.0)  # well past the budget below
            return _met_result(leaf.id)
        return _met_result(leaf.id)

    with patch(
        "app.policy.adjudicator.adjudicate_criterion",
        side_effect=fake_adjudicate,
    ):
        result = await adjudicate_all(
            criteria_root=root,
            exclusions=[],
            case=CaseFacts(bundle_facts=FactCollection()),
            leaf_budget_seconds=0.2,
        )

    assert set(result.leaf_verdicts.keys()) == {"A", "B", "C"}
    assert result.leaf_verdicts["A"].verdict == "met"
    assert result.leaf_verdicts["C"].verdict == "met"
    assert result.leaf_verdicts["B"].verdict == "unclear"
    assert "budget" in result.leaf_verdicts["B"].missing_info[0].lower()


@pytest.mark.asyncio
async def test_leaf_exception_does_not_kill_batch():
    """If one leaf raises, the batch still completes with that leaf as `unclear`."""
    root = CriterionNode(
        id="root", type="internal", operator="ALL", description="",
        children=[_leaf("A"), _leaf("B")],
    )

    async def fake_adjudicate(leaf, case, *, digest=None, policy=None):
        if leaf.id == "B":
            raise RuntimeError("simulated upstream failure")
        return _met_result(leaf.id)

    with patch(
        "app.policy.adjudicator.adjudicate_criterion",
        side_effect=fake_adjudicate,
    ):
        result = await adjudicate_all(
            criteria_root=root,
            exclusions=[],
            case=CaseFacts(bundle_facts=FactCollection()),
            leaf_budget_seconds=5.0,
        )

    assert result.leaf_verdicts["A"].verdict == "met"
    assert result.leaf_verdicts["B"].verdict == "unclear"
    assert "simulated upstream failure" in result.leaf_verdicts["B"].reasoning


@pytest.mark.asyncio
async def test_exclusion_timeout_is_handled():
    """Exclusions go through the same fallback path."""
    root = CriterionNode(
        id="root", type="internal", operator="ALL", description="",
        children=[_leaf("A")],
    )
    exclusion = Exclusion(
        id="X1",
        description="excluded condition",
        policy_citation=PolicyCitation(page=1, section="X", quote="stub quote"),
        evaluation={},
        verdict_rubric={"met": "excluded"},
    )

    async def fake_adjudicate(leaf, case, *, digest=None, policy=None):
        if leaf.id == "X1":
            await asyncio.sleep(5.0)
        return _met_result(leaf.id)

    with patch(
        "app.policy.adjudicator.adjudicate_criterion",
        side_effect=fake_adjudicate,
    ):
        result = await adjudicate_all(
            criteria_root=root,
            exclusions=[exclusion],
            case=CaseFacts(bundle_facts=FactCollection()),
            leaf_budget_seconds=0.2,
        )

    assert result.exclusion_verdicts["X1"].verdict == "unclear"
    assert result.leaf_verdicts["A"].verdict == "met"
