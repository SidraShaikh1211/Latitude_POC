"""Exhaustive unit tests for the rollup operators."""

import itertools

import pytest

from app.determination.rollup import _fold, collect_leaf_verdicts, rollup
from app.policy.registry import CriterionLeaf, CriterionNode, PolicyCitation


VERDICTS = ["met", "not_met", "unclear", "not_documented"]


def _leaf(id_: str) -> CriterionLeaf:
    return CriterionLeaf(
        id=id_, type="leaf", description="",
        policy_citation=PolicyCitation(page=1, section=None, quote="x"),
        evaluation={}, verdict_rubric={},
    )


def _node(id_: str, op: str, children, *, k: int | None = None) -> CriterionNode:
    return CriterionNode(
        id=id_, type="internal", operator=op,
        description="", children=children, k=k,
    )


# ---------------------------------------------------------------------------
# ALL
# ---------------------------------------------------------------------------

def test_all_with_all_met():
    assert _fold("ALL", ["met", "met", "met"]) == "met"


def test_all_with_one_not_met_dominates():
    for combo in [
        ["not_met", "met", "met"],
        ["met", "not_met", "unclear"],
        ["not_met", "unclear", "not_documented"],
    ]:
        assert _fold("ALL", combo) == "not_met"


def test_all_unclear_when_some_unclear_no_not_met():
    assert _fold("ALL", ["met", "unclear", "met"]) == "unclear"
    assert _fold("ALL", ["unclear", "not_documented", "met"]) == "unclear"


def test_all_not_documented_when_none_known():
    assert _fold("ALL", ["not_documented", "not_documented"]) == "not_documented"


# ---------------------------------------------------------------------------
# ONE_OF
# ---------------------------------------------------------------------------

def test_one_of_with_any_met():
    assert _fold("ONE_OF", ["not_met", "met"]) == "met"
    assert _fold("ONE_OF", ["unclear", "met", "not_documented"]) == "met"


def test_one_of_all_not_met():
    assert _fold("ONE_OF", ["not_met", "not_met"]) == "not_met"


def test_one_of_unclear_with_no_met():
    assert _fold("ONE_OF", ["not_met", "unclear", "not_documented"]) == "unclear"


def test_one_of_not_documented():
    assert _fold("ONE_OF", ["not_documented", "not_documented"]) == "not_documented"


# ---------------------------------------------------------------------------
# NOT (used for exclusions semantics)
# ---------------------------------------------------------------------------

def test_not_with_any_met_fails():
    assert _fold("NOT", ["met", "not_met"]) == "not_met"


def test_not_with_all_not_met_passes():
    assert _fold("NOT", ["not_met", "not_met"]) == "met"


def test_not_unclear():
    assert _fold("NOT", ["unclear", "not_met"]) == "unclear"


# ---------------------------------------------------------------------------
# AT_LEAST_K
# ---------------------------------------------------------------------------

def test_at_least_k_clear_pass():
    assert _fold("AT_LEAST_K", ["met", "met", "not_met"], k=2) == "met"


def test_at_least_k_clear_fail():
    # With k=2 and only met=1, even unclears flipping can't reach k? met=1 + unclear=0 = 1 < 2 → not_met
    assert _fold("AT_LEAST_K", ["met", "not_met", "not_met"], k=2) == "not_met"


def test_at_least_k_unclear_when_still_possible():
    # met=1, unclear=1, k=2: 1+1=2 >= k → unclear
    assert _fold("AT_LEAST_K", ["met", "unclear", "not_met"], k=2) == "unclear"


def test_at_least_k_requires_k():
    with pytest.raises(ValueError):
        _fold("AT_LEAST_K", ["met"], k=None)


def test_unknown_operator_raises():
    with pytest.raises(ValueError):
        _fold("MAJORITY", ["met"])


# ---------------------------------------------------------------------------
# Rollup tree traversal
# ---------------------------------------------------------------------------

def test_rollup_leaf_lookup():
    leaf = _leaf("L1")
    nv = rollup(leaf, {"L1": "met"})
    assert nv.verdict == "met"
    assert nv.children == []


def test_rollup_leaf_missing_treated_as_not_documented():
    leaf = _leaf("L1")
    nv = rollup(leaf, {})
    assert nv.verdict == "not_documented"


def test_rollup_two_level_tree():
    """root (ALL) → child1 (ONE_OF) → L1, L2 ; root → L3"""
    leaves = [_leaf("L1"), _leaf("L2"), _leaf("L3")]
    child1 = _node("child1", "ONE_OF", [leaves[0], leaves[1]])
    root = _node("root", "ALL", [child1, leaves[2]])

    nv = rollup(root, {"L1": "met", "L2": "not_met", "L3": "met"})
    # child1: one_of(met, not_met) = met; root: all(met, met) = met
    assert nv.verdict == "met"


def test_rollup_two_level_tree_root_not_met():
    leaves = [_leaf("L1"), _leaf("L2"), _leaf("L3")]
    child1 = _node("child1", "ONE_OF", [leaves[0], leaves[1]])
    root = _node("root", "ALL", [child1, leaves[2]])
    # child1 met; L3 not_met → root not_met (dominated)
    nv = rollup(root, {"L1": "met", "L2": "not_met", "L3": "not_met"})
    assert nv.verdict == "not_met"


def test_rollup_unclear_propagates():
    leaves = [_leaf("L1"), _leaf("L2")]
    root = _node("root", "ALL", leaves)
    nv = rollup(root, {"L1": "met", "L2": "unclear"})
    assert nv.verdict == "unclear"


def test_collect_leaf_verdicts():
    leaves = [_leaf("L1"), _leaf("L2"), _leaf("L3")]
    root = _node("root", "ALL", [_node("child", "ONE_OF", [leaves[0], leaves[1]]), leaves[2]])
    nv = rollup(root, {"L1": "met", "L2": "not_met", "L3": "unclear"})
    flat = collect_leaf_verdicts(nv)
    assert flat == {"L1": "met", "L2": "not_met", "L3": "unclear"}


# ---------------------------------------------------------------------------
# Property: ALL is monotone under refinement (more met never reduces verdict)
# ---------------------------------------------------------------------------

def test_all_is_monotone_in_met_count():
    """If we replace a not_documented with met, the ALL verdict can only
    move 'forward' on the lattice (not_documented < unclear < not_met=met
    at the same level for ALL semantics)."""
    base = ["unclear", "not_documented", "met"]
    upgraded = ["unclear", "met", "met"]
    assert _fold("ALL", base) == "unclear"
    assert _fold("ALL", upgraded) == "unclear"  # still has an unclear


# ---------------------------------------------------------------------------
# Smoke: every operator handles every singleton verdict
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op,v", [(op, v) for op in ["ALL", "ONE_OF", "NOT"] for v in VERDICTS])
def test_singleton_inputs(op, v):
    out = _fold(op, [v])
    assert out in VERDICTS
