"""Deterministic policy selector.

Inputs: a CaseContext (CPT, ICD-10s, payer, LOB, state, age, service date,
care setting) + a list of prior Procedure resources from the inbound Bundle
+ optional CaseFacts (intake-extracted facts, used only at the deepest tier).

Output: a SelectionResult with `status` (ok / no_match / needs_disambiguation),
the selected policy, branch (initial / repeat), an audit trail showing
which policies were eliminated, and `tiebreaker_used` naming the tier that
resolved the pick (when there was a real tie to resolve).

Filter pipeline (Tier 1) runs in three explicit phases so the audit trail
distinguishes the *kind* of mismatch. A policy must survive all three to
become a candidate:

  Phase A — CPT.    Drop policies whose CPT/HCPCS list does not include the
                    requested code. Wrong-procedure mismatches are tagged
                    `[cpt]` in the elimination log.

  Phase B — ICD-10. Drop policies whose ICD-10 patterns don't match any of
                    the *requested-indication* codes (from
                    Claim.item.diagnosisSequence — required by the parser,
                    so no fallback to the patient's full problem list).
                    Tagged `[icd10]`. This is the "right procedure, wrong
                    indication" elimination.

  Phase C — context. LOB, state, age, care setting, request category,
                    effective-date window. Tagged `[context]` — the policy
                    matches clinically but the case is out-of-scope on
                    geography/eligibility.

Disambiguation ladder — each tier only runs if the previous left ≥2 tied:

  Tier 1  CPT → ICD-10 → context filter (above).

  Tier 2  Static specificity — narrower CPT list, narrower state/LOB lists,
          age bounds, narrower setting list = more specific. Auto-pick if
          top exceeds runner-up by ≥ 1.5x.

  Tier 3  Case-aware ICD-10 match quality — literal-code match beats glob.
          For each requested-indication ICD-10, find the most-specific
          pattern the policy declares that matched it; sum the precision
          across the case's codes. Auto-pick if top ≥ 1.5x runner-up.

  Tier 4  ICD-10 pattern narrowness — measures how broad each policy's
          ICD-10 coverage is overall (sum of per-pattern breadth: 1 for a
          literal, 4 for a trailing-star glob, etc.). A policy with one
          glob covering a whole indication is broader than one with a
          handful of literals targeting a single indication. Auto-pick
          if top ≥ 1.2x runner-up. This is what resolves the
          multi-indication-patient single-indication-request case.

  Tier 5  Fact-coverage peek — for each policy's criteria tree, count
          leaves whose `fact_types_needed` are present in the intake-
          extracted facts. The policy whose criteria the case actually
          has evidence for wins by 1.2x margin. Skipped when intake
          facts are not provided.

If all five tiers leave ≥2 tied, status is `needs_disambiguation` — the
selector never silently picks on a real tie.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Literal

from app.pas.bundle_parser import CaseContext
from app.policy.registry import CriterionLeaf, CriterionNode, Policy, PolicyRegistry

if TYPE_CHECKING:
    from app.policy.tools import CaseFacts


SelectionStatus = Literal["ok", "no_match", "needs_disambiguation"]
Branch = Literal["initial", "repeat"]
Tiebreaker = Literal[
    "static_specificity",
    "icd10_match_quality",
    "icd10_pattern_narrowness",
    "fact_coverage",
]


@dataclass
class SelectionResult:
    status: SelectionStatus
    selected_policy_id: str | None = None
    branch: Branch | None = None
    candidates_considered: list[str] = field(default_factory=list)
    eliminated: list[tuple[str, str]] = field(default_factory=list)  # (policy_id, reason)
    selection_reason: str = ""
    tie_set: list[str] = field(default_factory=list)
    tiebreaker_used: Tiebreaker | None = None


# Auto-pick margins. Tighter for the precise tiers, looser for the fuzzier
# narrowness / fact-coverage tiers.
_MARGIN_SPECIFICITY = 1.5
_MARGIN_ICD10_QUALITY = 1.5
_MARGIN_ICD10_NARROWNESS = 1.2
_MARGIN_FACT_COVERAGE = 1.2
_EPS = 1e-9


def select_policy(
    context: CaseContext,
    *,
    registry: PolicyRegistry,
    prior_procedures: Iterable[dict] = (),
    case_facts: "CaseFacts | None" = None,
) -> SelectionResult:
    """Run the 4-tier ladder over all policies in the registry."""
    all_policies = registry.all_policies()
    if not all_policies:
        return SelectionResult(
            status="no_match",
            selection_reason="No policies loaded in registry.",
        )

    # --- Tier 1: filter (CPT → ICD-10 → context, in order) ----------------
    eliminated: list[tuple[str, str]] = []

    after_cpt: list[Policy] = []
    for p in all_policies:
        reason = _eliminate_by_cpt(p, context)
        if reason is None:
            after_cpt.append(p)
        else:
            eliminated.append((p.policy_id, f"[cpt] {reason}"))

    if not after_cpt:
        return SelectionResult(
            status="no_match",
            eliminated=eliminated,
            selection_reason=f"No policy covers CPT {context.cpt_code}.",
        )

    after_icd: list[Policy] = []
    for p in after_cpt:
        reason = _eliminate_by_icd10(p, context)
        if reason is None:
            after_icd.append(p)
        else:
            eliminated.append((p.policy_id, f"[icd10] {reason}"))

    if not after_icd:
        return SelectionResult(
            status="no_match",
            eliminated=eliminated,
            selection_reason=(
                "Policies cover the requested CPT but none match the "
                "requested-indication ICD-10 codes."
            ),
        )

    candidates: list[Policy] = []
    for p in after_icd:
        reason = _eliminate_by_context(p, context)
        if reason is None:
            candidates.append(p)
        else:
            eliminated.append((p.policy_id, f"[context] {reason}"))

    candidate_ids = [p.policy_id for p in candidates]

    if not candidates:
        return SelectionResult(
            status="no_match",
            eliminated=eliminated,
            selection_reason=(
                "Policies cover the requested CPT and indication but none "
                "match on context (LOB / state / age / setting / effective date)."
            ),
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

    # --- Tier 2: static specificity ----------------------------------------
    picked, reason, tied = _pick_by_score(
        candidates,
        scorer=_specificity_score,
        margin=_MARGIN_SPECIFICITY,
        label="static specificity",
    )
    if picked is not None:
        return _build_ok(
            picked, context, prior_procedures, candidate_ids, eliminated, reason,
            tiebreaker="static_specificity",
        )

    # --- Tier 3: case-aware ICD-10 match quality ---------------------------
    picked, reason, tied = _pick_by_score(
        tied,
        scorer=lambda p: _icd10_match_quality(p, context),
        margin=_MARGIN_ICD10_QUALITY,
        label="ICD-10 match quality",
    )
    if picked is not None:
        return _build_ok(
            picked, context, prior_procedures, candidate_ids, eliminated, reason,
            tiebreaker="icd10_match_quality",
        )

    # --- Tier 4: ICD-10 pattern narrowness ---------------------------------
    picked, reason, tied = _pick_by_score(
        tied,
        scorer=_icd10_pattern_narrowness,
        margin=_MARGIN_ICD10_NARROWNESS,
        label="ICD-10 pattern narrowness",
    )
    if picked is not None:
        return _build_ok(
            picked, context, prior_procedures, candidate_ids, eliminated, reason,
            tiebreaker="icd10_pattern_narrowness",
        )

    # --- Tier 5: fact-coverage peek ----------------------------------------
    if case_facts is not None and case_facts.extracted is not None:
        picked, reason, tied = _pick_by_score(
            tied,
            scorer=lambda p: _fact_coverage(p, case_facts),
            margin=_MARGIN_FACT_COVERAGE,
            label="fact coverage",
        )
        if picked is not None:
            return _build_ok(
                picked, context, prior_procedures, candidate_ids, eliminated, reason,
                tiebreaker="fact_coverage",
            )

    # --- Genuine ambiguity --------------------------------------------------
    tie_set = [p.policy_id for p in tied]
    return SelectionResult(
        status="needs_disambiguation",
        selected_policy_id=None,
        candidates_considered=candidate_ids,
        eliminated=eliminated,
        tie_set=tie_set,
        selection_reason=(
            f"Multiple policies tied through all disambiguation tiers: {tie_set}. "
            "Human review required."
        ),
    )


# ---------------------------------------------------------------------------
# Tier orchestration helpers
# ---------------------------------------------------------------------------


def _pick_by_score(
    candidates: list[Policy],
    *,
    scorer,
    margin: float,
    label: str,
) -> tuple[Policy | None, str, list[Policy]]:
    """Score each candidate, return (winner, reason, tied) or (None, _, tied).

    `tied` is always populated with the candidates that remain in contention
    (all of them at this score class if the tier didn't resolve, just the
    winner if it did) so the next tier can keep narrowing.
    """
    scored = [(p, scorer(p)) for p in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_score = scored[0][1]
    runner_up_score = scored[1][1] if len(scored) > 1 else 0.0

    if top_score / max(runner_up_score, _EPS) >= margin and top_score > 0:
        winner = scored[0][0]
        breakdown = ", ".join(f"{p.policy_id}={s:.2f}" for p, s in scored)
        reason = (
            f"Tier resolved by {label}: {winner.policy_id} score={top_score:.2f} "
            f"vs runner-up {scored[1][0].policy_id} score={runner_up_score:.2f} "
            f"(all: {breakdown})."
        )
        return winner, reason, [winner]

    # No clear winner — keep everyone within the margin in the tied set so
    # the next tier can try to break it further.
    threshold = top_score / margin
    tied = [p for p, s in scored if s >= threshold]
    if len(tied) < 2:
        # Safety net: at least two policies must remain to keep tiering.
        tied = [p for p, _ in scored]
    return None, "", tied


def _build_ok(
    chosen: Policy,
    context: CaseContext,
    prior_procedures: Iterable[dict],
    candidate_ids: list[str],
    eliminated: list[tuple[str, str]],
    reason: str,
    *,
    tiebreaker: Tiebreaker,
) -> SelectionResult:
    branch = _determine_branch(chosen, context, prior_procedures)
    return SelectionResult(
        status="ok",
        selected_policy_id=chosen.policy_id,
        branch=branch,
        candidates_considered=candidate_ids,
        eliminated=eliminated,
        selection_reason=f"{reason} branch={branch}.",
        tiebreaker_used=tiebreaker,
    )


# ---------------------------------------------------------------------------
# Tier 1 — filter (CPT → ICD-10 → context)
# ---------------------------------------------------------------------------
#
# Each phase returns either None (policy survives this phase) or a one-line
# reason it's eliminated. select_policy() walks the three phases in order so
# the audit log distinguishes "wrong procedure" / "wrong indication" /
# "right both, wrong context".
#
# Payer-name match is intentionally not enforced in any phase: in the
# current single-tenant demo every loaded policy belongs to "the payer," so
# requests are matched on the clinical filters only.


def _eliminate_by_cpt(p: Policy, ctx: CaseContext) -> str | None:
    """Phase A — does this policy cover the requested CPT/HCPCS?"""
    cpt_codes = set(p.applies_to.cpt_codes) | set(p.applies_to.hcpcs_codes)
    if cpt_codes and ctx.cpt_code not in cpt_codes:
        return f"CPT {ctx.cpt_code} not in policy CPTs"
    return None


def _eliminate_by_icd10(p: Policy, ctx: CaseContext) -> str | None:
    """Phase B — do any of the *requested-indication* ICD-10 codes match a
    policy ICD-10 pattern?

    Restricted to the diagnoses the requested line item is *for*
    (Claim.item.diagnosisSequence). The bundle parser enforces that this
    link is set, so the patient's full problem list never enters selection.
    """
    if not p.applies_to.icd10_patterns:
        return None
    indication_codes = ctx.requested_indication_icd10_codes
    if not any(
        _matches_glob(code, pat)
        for code in indication_codes
        for pat in p.applies_to.icd10_patterns
    ):
        return (
            f"none of requested-indication ICD-10 {indication_codes} "
            f"match patterns {p.applies_to.icd10_patterns}"
        )
    return None


def _eliminate_by_context(p: Policy, ctx: CaseContext) -> str | None:
    """Phase C — LOB, state, age, setting, request category, effective date."""
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
# Tier 2 — static specificity (more constraints + narrower lists = more specific)
# ---------------------------------------------------------------------------


def _specificity_score(p: Policy) -> float:
    """Higher = more specific. CPT narrowness is the dominant signal — a
    3-CPT policy is much more specialized than a 30-CPT policy. ICD-pattern
    narrowness gets its own tier (`_icd10_pattern_narrowness`) so it isn't
    drowned out by the CPT term when policies share the same CPT list."""
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
# Tier 3 — case-aware ICD-10 match quality
# ---------------------------------------------------------------------------


def _pattern_specificity(pattern: str) -> float:
    """Score how *precisely* a single ICD-10 pattern targets a code.

    Literal codes (no wildcards) are the strongest signal — the author
    enumerated each subcode they meant to cover. Globs are weaker, and
    trailing `*` globs (the most common shape) are weakest.
    """
    wildcards = pattern.count("*") + pattern.count("?")
    if wildcards == 0:
        return 10.0
    trailing_breadth = 1 if pattern.endswith("*") else 0
    return 10.0 / (1 + wildcards + 2 * trailing_breadth)


# The principal indication carries more weight than contributing secondaries
# — a single accidental literal match on a secondary code (e.g., dysmenorrhea
# riding along on an endometriosis hysterectomy request) shouldn't outscore
# a clean match on the primary indication.
_PRIMARY_WEIGHT = 2.0


def _icd10_match_quality(p: Policy, ctx: CaseContext) -> float:
    """Sum, over each requested-indication ICD-10, the best (most precise)
    pattern in the policy that matched it. The first code is treated as the
    principal indication and gets `_PRIMARY_WEIGHT`× the score; subsequent
    codes are contributing and weighted 1×. Policies with literal subcodes
    targeting the actual case codes beat policies with broad globs."""
    indication_codes = ctx.requested_indication_icd10_codes
    if not indication_codes or not p.applies_to.icd10_patterns:
        return 0.0
    total = 0.0
    for i, code in enumerate(indication_codes):
        weight = _PRIMARY_WEIGHT if i == 0 else 1.0
        best = 0.0
        for pat in p.applies_to.icd10_patterns:
            if _matches_glob(code, pat):
                best = max(best, _pattern_specificity(pat))
        total += weight * best
    return total


# ---------------------------------------------------------------------------
# Tier 4 — ICD-10 pattern narrowness (case-independent breadth measure)
# ---------------------------------------------------------------------------


def _pattern_breadth(pattern: str) -> float:
    """Inverse of pattern specificity: how broad the covered code space is.
    A literal targets exactly one code (breadth 1). A trailing-star glob
    like `N80.*` is much broader (breadth 4)."""
    return 10.0 / _pattern_specificity(pattern)


def _icd10_pattern_narrowness(p: Policy) -> float:
    """Higher = the policy's ICD-10 patterns cover a tighter code space
    overall. Used to distinguish single-indication policies (1 glob = 1
    indication) from policies that bundle multiple indications under the
    same CPT (multiple globs)."""
    patterns = p.applies_to.icd10_patterns
    if not patterns:
        return 0.0
    total_breadth = sum(_pattern_breadth(pat) for pat in patterns)
    return 10.0 / total_breadth


# ---------------------------------------------------------------------------
# Tier 4 — fact-coverage peek (deeper context check)
# ---------------------------------------------------------------------------


def _iter_leaves(node) -> Iterable[CriterionLeaf]:
    if isinstance(node, CriterionLeaf):
        yield node
        return
    if isinstance(node, CriterionNode):
        for child in node.children:
            yield from _iter_leaves(child)


def _present_fact_types(case_facts: "CaseFacts") -> set[str]:
    """Set of fact-type strings (matching the values policy leaves use in
    `evaluation.fact_types_needed`) that have at least one instance in the
    intake-extracted facts."""
    types: set[str] = set()
    ex = case_facts.extracted
    if ex is None:
        return types
    if ex.conditions:
        types.add("Condition")
    if ex.observations:
        types.add("Observation")
        # Some criteria use ClinicalImpression-style narrative; intake
        # captures these as Observations with structured text.
        types.add("ClinicalImpression")
    if ex.procedures:
        types.add("Procedure")
    if ex.allergies:
        types.add("AllergyIntolerance")
    if ex.diagnostic_reports:
        types.add("DiagnosticReport")
    if ex.medications:
        for m in ex.medications:
            types.add(m.resource_type)  # MedicationRequest / MedicationStatement
    return types


def _fact_coverage(p: Policy, case_facts: "CaseFacts") -> float:
    """Fraction of policy leaves with ≥1 declared `fact_types_needed`
    present in the extracted intake facts. Higher = the case has more
    evidence aligned with this policy's criteria tree."""
    present = _present_fact_types(case_facts)
    leaves = list(_iter_leaves(p.criteria))
    if not leaves:
        return 0.0
    supported = 0
    for leaf in leaves:
        needed = leaf.evaluation.get("fact_types_needed") or []
        if not needed:
            # Leaves with no declared fact types are trivially "supported" —
            # they don't pull the score in either direction.
            supported += 1
            continue
        if any(ft in present for ft in needed):
            supported += 1
    return supported / len(leaves)


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
