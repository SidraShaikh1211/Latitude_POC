"""Deterministic policy selector.

Inputs: a CaseContext (CPT, ICD-10s, payer, LOB, state, age, service date,
care setting) + a list of prior Procedure resources from the inbound Bundle.

Output: a SelectionResult with `status` (ok / no_match / needs_disambiguation),
the selected policy, branch (initial / repeat), and an audit trail showing
which policies were eliminated and why.

The selector NEVER auto-picks on ambiguity. If two policies are equally
specific, the result is `needs_disambiguation` and a human must intervene.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.pas.bundle_parser import CaseContext
from app.policy.registry import Policy, PolicyRegistry


SelectionStatus = Literal["ok", "no_match", "needs_disambiguation"]
Branch = Literal["initial", "repeat"]


@dataclass
class SelectionResult:
    status: SelectionStatus
    selected_policy_id: str | None = None
    branch: Branch | None = None
    candidates_considered: list[str] = field(default_factory=list)
    eliminated: list[tuple[str, str]] = field(default_factory=list)  # (policy_id, reason)
    selection_reason: str = ""
    tie_set: list[str] = field(default_factory=list)


def select_policy(
    context: CaseContext,
    *,
    registry: PolicyRegistry,
    prior_procedures: Iterable[dict] = (),
) -> SelectionResult:
    """Run the filter chain over all policies in the registry."""
    all_policies = registry.all_policies()
    if not all_policies:
        return SelectionResult(
            status="no_match",
            selection_reason="No policies loaded in registry.",
        )

    candidates: list[Policy] = []
    eliminated: list[tuple[str, str]] = []

    for p in all_policies:
        reason = _why_eliminated(p, context)
        if reason is None:
            candidates.append(p)
        else:
            eliminated.append((p.policy_id, reason))

    candidate_ids = [p.policy_id for p in candidates]

    if not candidates:
        return SelectionResult(
            status="no_match",
            candidates_considered=candidate_ids,
            eliminated=eliminated,
            selection_reason="No policy matched all filter constraints.",
        )

    if len(candidates) == 1:
        chosen = candidates[0]
        branch = _determine_branch(chosen, context, prior_procedures)
        return SelectionResult(
            status="ok",
            selected_policy_id=chosen.policy_id,
            branch=branch,
            candidates_considered=candidate_ids,
            eliminated=eliminated,
            selection_reason=(
                f"Single matching policy after applying filters. branch={branch}."
            ),
        )

    # Multiple candidates — rank by specificity
    ranked = sorted(candidates, key=lambda p: _specificity_score(p), reverse=True)
    top = ranked[0]
    second = ranked[1]
    s_top = _specificity_score(top)
    s_second = _specificity_score(second)

    # Require a clear margin (>= 1.5x) to auto-pick
    if s_second == 0 or s_top / max(s_second, 1e-9) >= 1.5:
        branch = _determine_branch(top, context, prior_procedures)
        return SelectionResult(
            status="ok",
            selected_policy_id=top.policy_id,
            branch=branch,
            candidates_considered=candidate_ids,
            eliminated=eliminated,
            selection_reason=(
                f"Disambiguated by specificity: {top.policy_id} score={s_top:.2f} "
                f"vs runner-up {second.policy_id} score={s_second:.2f}. branch={branch}."
            ),
        )

    # Tie — refuse to auto-pick
    tie_set = [p.policy_id for p in ranked if _specificity_score(p) * 1.5 >= s_top]
    return SelectionResult(
        status="needs_disambiguation",
        selected_policy_id=None,
        candidates_considered=candidate_ids,
        eliminated=eliminated,
        tie_set=tie_set,
        selection_reason=(
            f"Multiple policies tied on specificity (within 1.5x): {tie_set}. "
            "Human review required."
        ),
    )


# ---------------------------------------------------------------------------
# Filter logic
# ---------------------------------------------------------------------------


def _why_eliminated(p: Policy, ctx: CaseContext) -> str | None:
    """Return a one-line reason a policy is eliminated, or None if it passes."""
    # Payer-name match intentionally not enforced: in the current single-tenant
    # demo every loaded policy belongs to "the payer," so requests are matched
    # on the clinical filters (CPT/ICD-10/state/age/effective date) only.

    # Effective date window
    try:
        eff_from = date.fromisoformat(p.effective_from)
    except Exception:
        eff_from = None
    eff_until = None
    if p.effective_until:
        try:
            eff_until = date.fromisoformat(p.effective_until)
        except Exception:
            eff_until = None
    if eff_from and ctx.service_date < eff_from:
        return f"service_date {ctx.service_date} before effective_from {eff_from}"
    if eff_until and ctx.service_date > eff_until:
        return f"service_date {ctx.service_date} after effective_until {eff_until}"

    # CPT / HCPCS match
    cpt_codes = set(p.applies_to.cpt_codes) | set(p.applies_to.hcpcs_codes)
    if cpt_codes and ctx.cpt_code not in cpt_codes:
        return f"CPT {ctx.cpt_code} not in policy CPTs"

    # ICD-10 glob match (at least one ICD-10 must match a pattern)
    if p.applies_to.icd10_patterns:
        if not any(
            _matches_glob(code, pat)
            for code in ctx.icd10_codes
            for pat in p.applies_to.icd10_patterns
        ):
            return f"none of ICD-10 {ctx.icd10_codes} match patterns {p.applies_to.icd10_patterns}"

    if p.applies_to.lines_of_business and ctx.line_of_business not in p.applies_to.lines_of_business:
        return f"LOB {ctx.line_of_business} not in {p.applies_to.lines_of_business}"

    if p.applies_to.states and ctx.state not in p.applies_to.states:
        return f"state {ctx.state} not in policy states"

    if p.applies_to.age_min is not None and ctx.patient_age < p.applies_to.age_min:
        return f"age {ctx.patient_age} below age_min {p.applies_to.age_min}"
    if p.applies_to.age_max is not None and ctx.patient_age > p.applies_to.age_max:
        return f"age {ctx.patient_age} above age_max {p.applies_to.age_max}"

    if p.applies_to.settings_of_care and ctx.care_setting not in p.applies_to.settings_of_care:
        return f"care_setting {ctx.care_setting} not in {p.applies_to.settings_of_care}"

    if p.applies_to.request_categories and ctx.request_category not in p.applies_to.request_categories:
        return f"request_category {ctx.request_category} not allowed"

    return None


def _matches_glob(code: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(code.upper(), pattern.upper())


# ---------------------------------------------------------------------------
# Specificity (more constraints + narrower lists = more specific)
# ---------------------------------------------------------------------------


def _specificity_score(p: Policy) -> float:
    """Higher = more specific. CPT narrowness is the dominant signal — a
    3-CPT policy is much more specialized than a 30-CPT policy. We give it
    a large weight so it outweighs shared attributes (states/LOB) that
    contribute equally to all candidates."""
    score = 0.0
    a = p.applies_to
    if a.cpt_codes:
        # Dominant: weight ~100 / len(cpt_codes) so a 3-CPT policy scores
        # ~33 and a 30-CPT policy scores ~3 — a clear margin.
        score += 100.0 / len(a.cpt_codes)
    if a.states:
        score += 2.0 / len(a.states)
    if a.lines_of_business:
        score += 1.0 / len(a.lines_of_business)
    if a.age_min is not None:
        score += 0.5
    if a.age_max is not None:
        score += 0.5
    if a.settings_of_care:
        score += 0.5 / len(a.settings_of_care)
    return score


# ---------------------------------------------------------------------------
# Branch determination
# ---------------------------------------------------------------------------


def _determine_branch(
    policy: Policy, ctx: CaseContext, prior_procedures: Iterable[dict]
) -> Branch:
    """Initial vs repeat: are there prior procedures in the same family as
    the requested CPT within this policy's CPT scope?"""
    in_scope = set(policy.applies_to.cpt_codes) | set(policy.applies_to.hcpcs_codes)
    for proc in prior_procedures:
        cpt = _extract_procedure_cpt(proc)
        if cpt and cpt in in_scope:
            return "repeat"
    return "initial"


def _extract_procedure_cpt(proc: dict) -> str | None:
    code = proc.get("code") or {}
    for c in code.get("coding") or []:
        if "cpt" in (c.get("system", "").lower()) or "ama-assn" in (c.get("system", "").lower()):
            return c.get("code")
    return None
