"""Per-criterion adjudicator agent.

Given a single CriterionLeaf and the case facts, run a Claude agent loop
with the 5 adjudicator tools. The model's terminal output must be a JSON
`Verdict` matching the schema below; we coerce that via tool-use rather
than free-form JSON to keep the contract reliable.

The agent runs per-leaf; the orchestrator (adjudicate_all_leaves) fires all
leaves in parallel via asyncio.gather, with prompt caching on the system
prompt + tools so per-leaf cost stays bounded.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from app.llm.client import Usage, get_client
from app.policy.registry import CriterionLeaf, CriterionNode, Exclusion, iter_leaves
from app.policy.tools import CaseFacts, dispatch_tool, tool_definitions


log = structlog.get_logger()


Verdict = Literal["met", "not_met", "unclear", "not_documented"]


# ---------------------------------------------------------------------------
# Output schema for the adjudicator's final answer
# ---------------------------------------------------------------------------


class PatientEvidence(BaseModel):
    fhir_resource_id: str | None = None
    fact_type: str | None = None
    value_summary: str
    document_id: str | None = None
    page: int | None = None
    quote: str | None = None
    verified: bool = False


class CriterionVerdict(BaseModel):
    criterion_id: str
    verdict: Literal["met", "not_met", "unclear", "not_documented"]
    confidence: float = Field(ge=0.0, le=1.0)
    patient_evidence: list[PatientEvidence] = Field(default_factory=list)
    reasoning: str
    missing_info: list[str] = Field(default_factory=list)


@dataclass
class AdjudicationResult:
    verdict: CriterionVerdict
    iterations: int
    usage: Usage
    tool_calls: list[dict] = field(default_factory=list)


def _load_adjudicator_skill() -> str:
    path = Path(__file__).resolve().parent.parent.parent / "skills" / "pa-adjudicator" / "SKILL.md"
    return path.read_text()


def _build_criterion_payload(leaf: CriterionLeaf | Exclusion) -> str:
    """Render a criterion (leaf or exclusion) as user-message text for the agent."""
    cit = leaf.policy_citation
    rubric_lines = []
    for k in ("met", "not_met", "unclear", "not_documented"):
        v = leaf.verdict_rubric.get(k)
        if v:
            rubric_lines.append(f"  - {k}: {v}")

    parts = [
        f"criterion_id: {leaf.id}",
        f"description: {leaf.description}",
        f"policy_citation (must be referenced if you cite the policy):",
        f"  page: {cit.page}",
        f"  section: {cit.section}",
        f"  quote: {cit.quote!r}",
        "evaluation_hints:",
        f"  {json.dumps(leaf.evaluation, indent=2)}",
        "verdict_rubric:",
        *rubric_lines,
    ]
    return "\n".join(parts)


def _build_case_summary(case: CaseFacts) -> str:
    """A compact summary of what's in the case (the agent will pull more via tools)."""
    bf = case.bundle_facts
    summary = {
        "documents_available": list(case.documents.keys()),
        "patient_present": bf.patient is not None,
        "current_episode_diagnoses_in_bundle": _count(bf.conditions),
        "observations_in_bundle": _count(bf.observations),
        "medication_requests_in_bundle": _count(bf.medication_requests),
        "medication_statements_in_bundle": _count(bf.medication_statements),
        "prior_procedures_in_bundle": _count(bf.procedures_prior),
        "allergies_in_bundle": _count(bf.allergies),
    }
    if case.extracted is not None:
        summary["intake_extracted"] = {
            "conditions": len(case.extracted.conditions),
            "observations": len(case.extracted.observations),
            "medications": len(case.extracted.medications),
            "procedures": len(case.extracted.procedures),
            "allergies": len(case.extracted.allergies),
            "diagnostic_reports": len(case.extracted.diagnostic_reports),
        }
    return json.dumps(summary, indent=2)


def _count(items: list) -> int:
    return len(items) if items else 0


# ---------------------------------------------------------------------------
# Per-criterion adjudication
# ---------------------------------------------------------------------------


async def adjudicate_criterion(
    leaf: CriterionLeaf | Exclusion,
    case: CaseFacts,
    *,
    max_iterations: int = 8,
) -> AdjudicationResult:
    """Run the agent loop for a single criterion."""
    client = get_client()
    system = _load_adjudicator_skill()
    user = (
        "Evaluate the following policy criterion against the patient case.\n\n"
        "CRITERION:\n"
        f"{_build_criterion_payload(leaf)}\n\n"
        "CASE SUMMARY:\n"
        f"{_build_case_summary(case)}\n\n"
        "Use the available tools to inspect the case. When you have enough evidence, "
        "return a CriterionVerdict via the `return_criterionverdict` tool. Do not free-form text the answer."
    )

    # Wire the adjudicator tools (5) + the structured-output return tool
    adj_tools = tool_definitions()
    return_tool = {
        "name": "return_criterionverdict",
        "description": (
            "Submit the final CriterionVerdict for this criterion. Calling this "
            "ends the adjudication loop for this criterion."
        ),
        "input_schema": CriterionVerdict.model_json_schema(),
    }
    tools = adj_tools + [return_tool]

    captured: dict[str, Any] = {"verdict": None}

    async def tool_handler(name: str, payload: dict) -> Any:
        if name == "return_criterionverdict":
            captured["verdict"] = payload
            return {"ok": True}
        return dispatch_tool(case, name, payload)

    trace = await client.agent_loop(
        system=system,
        user=user,
        tools=tools,
        tool_handler=tool_handler,
        max_iterations=max_iterations,
        max_tokens=4096,
        cache_system=True,
        cache_tools=True,
        final_tool_name="return_criterionverdict",
    )

    if captured["verdict"] is None:
        # Agent never returned a verdict; treat as unclear with a structured note
        log.warning("adjudicator.no_verdict", criterion_id=leaf.id, iterations=trace.iterations)
        verdict = CriterionVerdict(
            criterion_id=leaf.id,
            verdict="unclear",
            confidence=0.2,
            reasoning=f"Adjudicator did not return a verdict within {max_iterations} iterations.",
            missing_info=["adjudicator timeout — re-run or escalate"],
        )
    else:
        try:
            verdict = CriterionVerdict.model_validate(captured["verdict"])
        except Exception as e:
            log.error("adjudicator.invalid_output", criterion_id=leaf.id, error=str(e))
            verdict = CriterionVerdict(
                criterion_id=leaf.id,
                verdict="unclear",
                confidence=0.2,
                reasoning=f"Adjudicator returned an invalid CriterionVerdict: {e}",
                missing_info=["malformed adjudicator output"],
            )

    # Patient-side citation verification: re-check every quote against the source doc
    verdict = _verify_patient_evidence(verdict, case)

    log.info(
        "adjudicator.done",
        criterion_id=leaf.id,
        verdict=verdict.verdict,
        confidence=verdict.confidence,
        iterations=trace.iterations,
        cache_read=trace.usage.cache_read_tokens,
    )

    return AdjudicationResult(
        verdict=verdict,
        iterations=trace.iterations,
        usage=trace.usage,
        tool_calls=trace.tool_calls,
    )


def _verify_patient_evidence(
    verdict: CriterionVerdict, case: CaseFacts
) -> CriterionVerdict:
    """Re-check every patient_evidence.quote against its cited document. Drop
    any unverifiable quotes (downgrade `met` to `unclear` if all evidence drops)."""
    from app.llm.citation_verify import verify_substring

    kept: list[PatientEvidence] = []
    dropped: list[str] = []
    for ev in verdict.patient_evidence:
        if not ev.document_id or not ev.quote or ev.page is None:
            kept.append(ev)
            continue
        doc = case.documents.get(ev.document_id)
        if not doc:
            dropped.append(f"missing doc {ev.document_id}")
            continue
        pt = doc.page(ev.page)
        if not pt:
            dropped.append(f"page {ev.page} out of range for {ev.document_id}")
            continue
        check = verify_substring(ev.quote, pt.normalized)
        if check.found:
            ev.verified = True
            kept.append(ev)
        else:
            dropped.append(f"unverifiable quote on p.{ev.page}: {ev.quote[:60]!r}")

    if dropped:
        log.warning(
            "adjudicator.dropped_evidence",
            criterion_id=verdict.criterion_id,
            dropped=dropped,
        )

    verdict.patient_evidence = kept
    # Safety: if the model said `met` but no objective evidence survived, downgrade.
    if verdict.verdict == "met" and not kept:
        verdict.verdict = "unclear"
        verdict.confidence = min(verdict.confidence, 0.5)
        verdict.reasoning += (
            " [auto-downgrade from met → unclear: no verifiable evidence after citation check]"
        )
    return verdict


# ---------------------------------------------------------------------------
# Parallel adjudication of the whole criteria tree + exclusions
# ---------------------------------------------------------------------------


@dataclass
class FullAdjudication:
    leaf_verdicts: dict[str, CriterionVerdict]
    exclusion_verdicts: dict[str, CriterionVerdict]
    escalations: list[str]
    total_usage: Usage
    iterations: dict[str, int]


async def adjudicate_all(
    *,
    criteria_root: CriterionNode | CriterionLeaf,
    exclusions: Iterable[Exclusion],
    case: CaseFacts,
    branch: str | None = None,
    parallel: int = 8,
) -> FullAdjudication:
    """Adjudicate every leaf in the tree and every exclusion in parallel.

    If `branch` is "initial" or "repeat", we filter the tree to only the
    leaves under the corresponding indication subtree (plus eligibility,
    severity, frequency, etc. — anything not under indication.X is shared).
    """
    leaves = list(iter_leaves(criteria_root))
    if branch:
        leaves = _filter_leaves_for_branch(leaves, branch)

    exclusions = list(exclusions)
    log.info("adjudicator.start", leaf_count=len(leaves), exclusion_count=len(exclusions),
             branch=branch, parallel=parallel)

    semaphore = asyncio.Semaphore(parallel)

    async def _run(node: CriterionLeaf | Exclusion) -> tuple[str, AdjudicationResult]:
        async with semaphore:
            r = await adjudicate_criterion(node, case)
            return node.id, r

    leaf_tasks = [asyncio.create_task(_run(l)) for l in leaves]
    excl_tasks = [asyncio.create_task(_run(e)) for e in exclusions]
    leaf_results = await asyncio.gather(*leaf_tasks, return_exceptions=False)
    excl_results = await asyncio.gather(*excl_tasks, return_exceptions=False)

    leaf_verdicts: dict[str, CriterionVerdict] = {}
    excl_verdicts: dict[str, CriterionVerdict] = {}
    iterations: dict[str, int] = {}
    total_usage = Usage()

    for nid, r in leaf_results:
        leaf_verdicts[nid] = r.verdict
        iterations[nid] = r.iterations
        total_usage.add(r.usage)
    for nid, r in excl_results:
        excl_verdicts[nid] = r.verdict
        iterations[nid] = r.iterations
        total_usage.add(r.usage)

    log.info(
        "adjudicator.done_all",
        leaves=len(leaf_verdicts),
        exclusions=len(excl_verdicts),
        escalations=len(case.escalations),
        total_tokens_in=total_usage.input_tokens,
        total_tokens_out=total_usage.output_tokens,
        cache_read=total_usage.cache_read_tokens,
        cost_usd=round(total_usage.cost_usd, 4),
    )

    return FullAdjudication(
        leaf_verdicts=leaf_verdicts,
        exclusion_verdicts=excl_verdicts,
        escalations=list(case.escalations),
        total_usage=total_usage,
        iterations=iterations,
    )


def _filter_leaves_for_branch(leaves: list[CriterionLeaf], branch: str) -> list[CriterionLeaf]:
    """Drop leaves under indication.repeat_injection.* when branch=initial,
    and vice versa. Everything else (eligibility, frequency_and_location) stays."""
    out: list[CriterionLeaf] = []
    for l in leaves:
        if branch == "initial" and ".repeat_injection." in l.id:
            continue
        if branch == "repeat" and ".initial_injection." in l.id:
            continue
        out.append(l)
    return out
