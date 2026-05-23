"""Deterministic tree rollup.

Takes a CriterionNode tree and a dict of leaf verdicts, walks the tree
recursively, and produces a verdict for every internal node — culminating
in a root verdict.

The four-valued verdict logic per operator is documented inline; this file
is intentionally pure and exhaustively unit-tested so adjudicator quirks
cannot corrupt the rollup.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from app.policy.registry import CriterionLeaf, CriterionNode


Verdict = Literal["met", "not_met", "unclear", "not_documented"]


@dataclass
class NodeVerdict:
    """The folded verdict for one node (leaf or internal), plus a list of
    child verdicts for trace/visualization."""

    node_id: str
    verdict: Verdict
    operator: str | None = None  # None for leaves
    children: list["NodeVerdict"] = field(default_factory=list)
    note: str = ""


def rollup(
    node: CriterionNode | CriterionLeaf,
    leaf_verdicts: Mapping[str, str],
) -> NodeVerdict:
    """Fold leaf verdicts into a single root NodeVerdict tree."""
    if isinstance(node, CriterionLeaf):
        v: Verdict = leaf_verdicts.get(node.id, "not_documented")  # type: ignore[assignment]
        if v not in {"met", "not_met", "unclear", "not_documented"}:
            v = "not_documented"
        return NodeVerdict(node_id=node.id, verdict=v)

    # Internal node — recurse first
    child_verdicts = [rollup(c, leaf_verdicts) for c in node.children]
    child_vals = [cv.verdict for cv in child_verdicts]
    folded = _fold(node.operator, child_vals, k=node.k)
    return NodeVerdict(
        node_id=node.id,
        verdict=folded,
        operator=node.operator,
        children=child_verdicts,
    )


def _fold(op: str, children: list[Verdict], *, k: int | None = None) -> Verdict:
    """Reduce a list of child verdicts under a single operator."""
    if not children:
        return "not_documented"

    if op == "ALL":
        # ALL: every child must be met. A single not_met dominates (deny).
        # Otherwise unclear/not_documented propagate.
        if all(v == "met" for v in children):
            return "met"
        if any(v == "not_met" for v in children):
            return "not_met"
        if any(v == "unclear" for v in children):
            return "unclear"
        return "not_documented"

    if op == "ONE_OF":
        # ONE_OF: at least one met = met. If all not_met, not_met.
        # Any unclear with no met = unclear.
        if any(v == "met" for v in children):
            return "met"
        if all(v == "not_met" for v in children):
            return "not_met"
        if any(v == "unclear" for v in children):
            return "unclear"
        return "not_documented"

    if op == "NOT":
        # NOT: passes if NO child is met (used for exclusions).
        # Note: in this codebase exclusions are evaluated as a flat list
        # in the Decider, not via this operator. NOT is still implemented
        # for completeness and tested.
        if any(v == "met" for v in children):
            return "not_met"
        if all(v == "not_met" for v in children):
            return "met"
        if any(v == "unclear" for v in children):
            return "unclear"
        return "not_documented"

    if op == "AT_LEAST_K":
        if k is None or k < 1:
            raise ValueError(f"AT_LEAST_K requires k>=1, got {k!r}")
        met_count = sum(1 for v in children if v == "met")
        unclear_count = sum(1 for v in children if v == "unclear")
        if met_count >= k:
            return "met"
        # If even with all unclears flipping to met we can't reach k, it's not_met.
        if met_count + unclear_count < k:
            return "not_met"
        return "unclear"

    raise ValueError(f"Unknown operator: {op!r}")


def collect_leaf_verdicts(rolled: NodeVerdict) -> dict[str, Verdict]:
    """Walk a NodeVerdict tree and return {leaf_id: verdict} for every leaf."""
    out: dict[str, Verdict] = {}

    def visit(n: NodeVerdict) -> None:
        if not n.children:
            out[n.node_id] = n.verdict
        else:
            for c in n.children:
                visit(c)

    visit(rolled)
    return out
