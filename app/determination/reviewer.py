"""Reviewer agent.

Inputs: the deterministic outcome (from Decider), every leaf verdict (from
Adjudicator), the policy, and the case facts.

Outputs: a structured ReviewerOutput containing
  - narrative: clinician-readable explanation
  - missing_info: refined single-question info requests for `pend` cases
  - key_evidence_cited: verbatim quotes that anchor the narrative
  - human_review_flag: set only when reviewer believes the verdict is wrong
    on its face (escalation; does NOT override the decision)

5-tool surface, 10-iteration budget. Cannot override the verdict.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field

from app.determination.decider import Determination
from app.llm.client import Usage, get_client
from app.policy.adjudicator import CriterionVerdict
from app.policy.registry import Exclusion, Policy, iter_leaves
from app.policy.tools import CaseFacts, dispatch_tool as adj_dispatch
from app.llm.citation_verify import verify_substring


log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


class MissingInfo(BaseModel):
    id: str
    criterion_id: str
    request: str = Field(min_length=15)


class EvidenceCitation(BaseModel):
    document_id: str | None = None
    page: int | None = None
    quote: str
    purpose: str


class ReviewerOutput(BaseModel):
    narrative: str = Field(min_length=80)
    missing_info: list[MissingInfo] = Field(default_factory=list)
    key_evidence_cited: list[EvidenceCitation] = Field(default_factory=list)
    human_review_flag: bool = False
    human_review_reason: str | None = None


@dataclass
class ReviewResult:
    output: ReviewerOutput
    iterations: int
    usage: Usage
    tool_calls: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reviewer-specific tools (some overlap with adjudicator tools intentionally)
# ---------------------------------------------------------------------------


def _reviewer_tool_definitions() -> list[dict]:
    return [
        {
            "name": "get_policy_section",
            "description": (
                "Fetch the verbatim policy text + section name for a given "
                "criterion_id or exclusion_id. Use this when you want to quote "
                "the policy in the narrative."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                },
                "required": ["criterion_id"],
            },
        },
        {
            "name": "get_patient_facts",
            "description": (
                "Pull structured patient facts by type. Use for narrative "
                "summarization (counting medications, listing diagnoses)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "fact_type": {"type": "string"},
                    "filters": {"type": "object", "additionalProperties": True},
                },
                "required": ["fact_type"],
            },
        },
        {
            "name": "get_document_excerpt",
            "description": "Fetch source-document text for verbatim quoting in narrative.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "page": {"type": "integer", "minimum": 1},
                    "context_chars": {"type": "integer", "minimum": 0, "default": 200},
                },
                "required": ["document_id", "page"],
            },
        },
        {
            "name": "draft_clinician_question",
            "description": (
                "Generate a polished missing-info request for ONE criterion. "
                "The request must be answerable in a single provider response."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "criterion_id": {"type": "string"},
                    "gap_summary": {"type": "string", "minLength": 20},
                },
                "required": ["criterion_id", "gap_summary"],
            },
        },
        {
            "name": "flag_for_human_review",
            "description": (
                "Flag the case for senior human review. Use ONLY when you "
                "believe the deterministic outcome is wrong on its face — not "
                "for routine pend/ambiguity. This does NOT override the outcome."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "minLength": 15},
                },
                "required": ["reason"],
            },
        },
    ]


def _reviewer_dispatch(
    case: CaseFacts,
    policy: Policy,
    leaf_index: dict[str, Any],
    exclusion_index: dict[str, Exclusion],
    flags: list[str],
    name: str,
    payload: dict[str, Any],
) -> Any:
    if name == "get_policy_section":
        cid = payload.get("criterion_id", "")
        if cid in exclusion_index:
            ex = exclusion_index[cid]
            return {
                "id": ex.id,
                "kind": "exclusion",
                "description": ex.description,
                "policy_citation": {
                    "page": ex.policy_citation.page,
                    "section": ex.policy_citation.section,
                    "quote": ex.policy_citation.quote,
                },
            }
        node = leaf_index.get(cid)
        if node is None:
            return {"error": f"unknown criterion_id {cid!r}"}
        cit = node.policy_citation
        return {
            "id": node.id,
            "kind": "leaf",
            "description": node.description,
            "policy_citation": {
                "page": cit.page,
                "section": cit.section,
                "quote": cit.quote,
            },
            "verdict_rubric": node.verdict_rubric,
        }

    if name == "get_patient_facts":
        return adj_dispatch(
            case, "search_facts_by_type",
            {"fact_type": payload.get("fact_type", ""), "filters": payload.get("filters")},
        )

    if name == "get_document_excerpt":
        return adj_dispatch(case, "get_document_excerpt", payload)

    if name == "draft_clinician_question":
        cid = payload.get("criterion_id", "")
        gap = payload.get("gap_summary", "")
        return {
            "criterion_id": cid,
            "draft_request": (
                f"Regarding {cid}: please provide documentation that resolves the "
                f"following gap — {gap}. A single response covering this point is sufficient."
            ),
        }

    if name == "flag_for_human_review":
        reason = payload.get("reason", "")
        flags.append(reason)
        return {"flagged": True, "reason": reason}

    return {"error": f"unknown tool {name!r}"}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _load_reviewer_skill() -> str:
    path = Path(__file__).resolve().parent.parent.parent / "skills" / "pa-reviewer" / "SKILL.md"
    return path.read_text()


def _build_state_payload(
    *,
    policy: Policy,
    determination: Determination,
    leaf_verdicts: dict[str, CriterionVerdict],
    exclusion_verdicts: dict[str, CriterionVerdict],
    case: CaseFacts,
) -> str:
    # Summarize verdicts compactly for the prompt
    notable = []
    for cid, v in sorted(leaf_verdicts.items()):
        if v.verdict in {"not_met", "unclear"}:
            notable.append({
                "criterion_id": cid,
                "verdict": v.verdict,
                "confidence": v.confidence,
                "reasoning": v.reasoning[:240],
                "missing_info": v.missing_info,
            })
    triggered_excl = [
        {
            "exclusion_id": eid,
            "verdict": v.verdict,
            "reasoning": v.reasoning[:240],
        }
        for eid, v in exclusion_verdicts.items()
        if v.verdict == "met"
    ]
    state = {
        "policy_id": policy.policy_id,
        "policy_name": policy.name,
        "outcome": determination.outcome,
        "outcome_rationale": determination.rationale,
        "triggered_exclusions": triggered_excl,
        "notable_criterion_verdicts": notable,
        "escalation_reasons_from_adjudicator": determination.escalation_reasons,
        "documents_available": list(case.documents.keys()),
    }
    return json.dumps(state, indent=2)


async def review_case(
    *,
    policy: Policy,
    determination: Determination,
    leaf_verdicts: dict[str, CriterionVerdict],
    exclusion_verdicts: dict[str, CriterionVerdict],
    case: CaseFacts,
    max_iterations: int = 10,
) -> ReviewResult:
    """Run the Reviewer agent and produce a ReviewerOutput."""
    client = get_client()
    system = _load_reviewer_skill()

    leaf_index = {l.id: l for l in iter_leaves(policy.criteria)}
    exclusion_index = {e.id: e for e in policy.exclusions}

    state_payload = _build_state_payload(
        policy=policy,
        determination=determination,
        leaf_verdicts=leaf_verdicts,
        exclusion_verdicts=exclusion_verdicts,
        case=case,
    )

    user = (
        "Write the narrative and (if outcome is `pend`) refine the missing-info "
        "requests for the following PA case. The deterministic outcome is final; "
        "you cannot change it.\n\n"
        f"CASE STATE:\n{state_payload}\n\n"
        "When ready, return a ReviewerOutput via `return_revieweroutput`."
    )

    review_tools = _reviewer_tool_definitions()
    return_tool = {
        "name": "return_revieweroutput",
        "description": "Submit the final ReviewerOutput.",
        "input_schema": ReviewerOutput.model_json_schema(),
    }
    tools = review_tools + [return_tool]

    captured: dict[str, Any] = {"output": None}
    flags: list[str] = []

    async def tool_handler(name: str, payload: dict) -> Any:
        if name == "return_revieweroutput":
            captured["output"] = payload
            return {"ok": True}
        return _reviewer_dispatch(
            case, policy, leaf_index, exclusion_index, flags, name, payload
        )

    trace = await client.agent_loop(
        system=system,
        user=user,
        tools=tools,
        tool_handler=tool_handler,
        max_iterations=max_iterations,
        max_tokens=4096,
        cache_system=True,
        cache_tools=True,
        final_tool_name="return_revieweroutput",
    )

    if captured["output"] is None:
        log.warning("reviewer.no_output", iterations=trace.iterations)
        output = ReviewerOutput(
            narrative=_fallback_narrative(
                determination,
                "Reviewer agent did not return a structured output within the "
                "iteration budget; narrative was auto-generated from the "
                "deterministic outcome and rationale.",
            ),
            missing_info=[],
            key_evidence_cited=[],
            human_review_flag=True,
            human_review_reason="Reviewer agent did not complete; manual review needed.",
        )
    else:
        try:
            output = ReviewerOutput.model_validate(captured["output"])
        except Exception as e:
            log.error("reviewer.invalid_output", error=str(e))
            output = ReviewerOutput(
                narrative=_fallback_narrative(
                    determination,
                    f"Reviewer returned a structured output that failed schema "
                    f"validation ({e}); this narrative was auto-generated as a "
                    "safe substitute.",
                ),
                human_review_flag=True,
                human_review_reason=f"Reviewer output failed validation: {e}",
            )

    # If the agent flagged for human review via the tool, copy that across
    if flags and not output.human_review_flag:
        output.human_review_flag = True
        output.human_review_reason = "; ".join(flags)

    # Verify any verbatim quotes in key_evidence_cited
    output = _verify_reviewer_quotes(output, case)

    log.info(
        "reviewer.done",
        outcome=determination.outcome,
        narrative_chars=len(output.narrative),
        missing_info_count=len(output.missing_info),
        evidence_count=len(output.key_evidence_cited),
        human_flag=output.human_review_flag,
        iterations=trace.iterations,
    )

    return ReviewResult(
        output=output,
        iterations=trace.iterations,
        usage=trace.usage,
        tool_calls=trace.tool_calls,
    )


_NARRATIVE_MIN_LENGTH = (
    ReviewerOutput.model_json_schema()["properties"]["narrative"]["minLength"]
)


def _fallback_narrative(determination: Determination, suffix: str) -> str:
    """Build an auto-generated narrative that is guaranteed to satisfy
    `ReviewerOutput.narrative` min_length. Composes the deterministic outcome,
    its rationale, and an explanatory suffix; pads with a trailing notice if
    the rationale is unusually terse so Pydantic validation cannot fail."""
    body = (
        f"Outcome: {determination.outcome}. {determination.rationale.strip()} "
        f"{suffix.strip()}"
    ).strip()
    if len(body) >= _NARRATIVE_MIN_LENGTH:
        return body
    padding = (
        " A medical reviewer should read the case and provide the final "
        "clinician-facing narrative."
    )
    return (body + padding).strip()


def _verify_reviewer_quotes(output: ReviewerOutput, case: CaseFacts) -> ReviewerOutput:
    kept: list[EvidenceCitation] = []
    for ev in output.key_evidence_cited:
        if not ev.document_id or ev.page is None:
            kept.append(ev)
            continue
        doc = case.documents.get(ev.document_id)
        if not doc:
            continue
        pt = doc.page(ev.page)
        if not pt:
            continue
        if verify_substring(ev.quote, pt.normalized).found:
            kept.append(ev)
        else:
            log.warning(
                "reviewer.dropped_quote",
                page=ev.page,
                doc=ev.document_id,
                quote=ev.quote[:60],
            )
    output.key_evidence_cited = kept
    return output
