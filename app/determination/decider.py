"""Final determination logic.

Combines the criteria-tree root verdict and the per-exclusion verdicts into
a single outcome:

    needs_human_review    — any explicit escalation flag was raised
    deny                  — any exclusion is `met` (short-circuit)
    deny                  — root verdict is `not_met`
    approve               — root verdict is `met` and no exclusion is `met`
    pend                  — root verdict is `unclear` or `not_documented`
                            (the case can become approve-able with more info)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal


Outcome = Literal["approve", "deny", "pend", "needs_human_review"]


@dataclass
class Determination:
    outcome: Outcome
    rationale: str
    triggered_exclusions: list[str]   # exclusion IDs that fired
    escalation_reasons: list[str]


def decide(
    *,
    root_verdict: str,
    exclusion_verdicts: Mapping[str, str],
    escalations: Iterable[str] = (),
) -> Determination:
    """Apply the determination decision tree.

    Args:
      root_verdict: one of {met, not_met, unclear, not_documented}.
      exclusion_verdicts: {exclusion_id: verdict}; any "met" → deny.
      escalations: any structured escalation reasons (e.g., from
        `request_human_review` tool calls).
    """
    escalation_list = [e for e in escalations if e]
    if escalation_list:
        return Determination(
            outcome="needs_human_review",
            rationale="Explicit escalation requested by an evaluator.",
            triggered_exclusions=[],
            escalation_reasons=escalation_list,
        )

    triggered = [eid for eid, v in exclusion_verdicts.items() if v == "met"]
    if triggered:
        return Determination(
            outcome="deny",
            rationale=(
                "Exclusion criteria met — request denied. "
                f"Triggered: {', '.join(triggered)}."
            ),
            triggered_exclusions=triggered,
            escalation_reasons=[],
        )

    if root_verdict == "met":
        return Determination(
            outcome="approve",
            rationale="All medical-necessity criteria satisfied; no exclusion fired.",
            triggered_exclusions=[],
            escalation_reasons=[],
        )

    if root_verdict == "not_met":
        return Determination(
            outcome="deny",
            rationale="Medical-necessity criteria not satisfied.",
            triggered_exclusions=[],
            escalation_reasons=[],
        )

    # unclear or not_documented → pend (provider can supply more info)
    return Determination(
        outcome="pend",
        rationale=(
            "Medical-necessity determination is incomplete. Specific information "
            "is required to render an approve/deny decision."
        ),
        triggered_exclusions=[],
        escalation_reasons=[],
    )
