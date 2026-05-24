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
from typing import Any

import structlog

from app.llm.client import Usage, get_client
from app.policy.deterministic_eval import try_deterministic_verdict
from app.policy.evidence_digest import build_evidence_digest
from app.policy.registry import (
    CriterionLeaf,
    CriterionNode,
    Exclusion,
    Policy,
    iter_leaves,
)
from app.policy.tools import CaseFacts, dispatch_tool, tool_definitions
from app.policy.verdict import CriterionVerdict, PatientEvidence, Verdict


log = structlog.get_logger()


# CriterionVerdict / PatientEvidence are re-exported from app.policy.verdict
# so existing call sites (`from app.policy.adjudicator import CriterionVerdict`)
# keep working without an import cycle now that deterministic_eval also needs
# them.
__all__ = [
    "CriterionVerdict",
    "PatientEvidence",
    "Verdict",
    "AdjudicationResult",
    "FullAdjudication",
    "adjudicate_criterion",
    "adjudicate_all",
]


@dataclass
class AdjudicationResult:
    verdict: CriterionVerdict
    iterations: int
    usage: Usage
    tool_calls: list[dict] = field(default_factory=list)


def _load_adjudicator_skill() -> str:
    path = Path(__file__).resolve().parent.parent.parent / "skills" / "pa-adjudicator" / "SKILL.md"
    return path.read_text()


def _build_criterion_payload(
    leaf: CriterionLeaf | Exclusion, policy: Policy | None = None
) -> str:
    """Render a criterion (leaf or exclusion) as user-message text for the agent."""
    cit = leaf.policy_citation
    rubric_lines = []
    for k in ("met", "not_met", "unclear", "not_documented"):
        v = leaf.verdict_rubric.get(k)
        if v:
            rubric_lines.append(f"  - {k}: {v}")

    # G: if the registry verified this citation, include the surrounding page
    # text so the agent doesn't need a tool round-trip to look up the policy.
    context_block = ""
    if policy is not None and cit.start_offset is not None and cit.end_offset is not None:
        page_text = policy.page_text(cit.page)
        if page_text:
            lo = max(0, cit.start_offset - 250)
            hi = min(len(page_text), cit.end_offset + 250)
            span = page_text[lo:hi]
            context_block = (
                "policy_context (verified surrounding text — do not re-fetch):\n"
                f"  {span!r}\n"
            )

    parts = [
        f"criterion_id: {leaf.id}",
        f"description: {leaf.description}",
        "policy_citation (must be referenced if you cite the policy):",
        f"  page: {cit.page}",
        f"  section: {cit.section}",
        f"  quote: {cit.quote!r}",
    ]
    if context_block:
        parts.append(context_block.rstrip())
    parts.extend([
        "evaluation_hints:",
        f"  {json.dumps(leaf.evaluation, indent=2)}",
        "verdict_rubric:",
        *rubric_lines,
    ])
    return "\n".join(parts)


def _build_system_blocks(skill_text: str, digest: str) -> list[dict]:
    """Two cached system blocks: skill (shared across all cases) and case
    digest (shared across all leaves of this case). Each gets its own cache
    breakpoint, so all parallel leaves read the digest from prompt cache."""
    blocks: list[dict] = [{
        "type": "text",
        "text": skill_text,
        "cache_control": {"type": "ephemeral"},
    }]
    if digest:
        blocks.append({
            "type": "text",
            "text": digest,
            "cache_control": {"type": "ephemeral"},
        })
    return blocks


# ---------------------------------------------------------------------------
# Per-criterion adjudication
# ---------------------------------------------------------------------------


async def adjudicate_criterion(
    leaf: CriterionLeaf | Exclusion,
    case: CaseFacts,
    *,
    max_iterations: int = 8,
    digest: str | None = None,
    policy: Policy | None = None,
) -> AdjudicationResult:
    """Run the agent loop for a single criterion.

    `digest` (optional) is a pre-built per-case evidence digest. When supplied
    it is placed in a cached system block so parallel leaves share it via
    prompt cache. When omitted we build it on the fly (e.g., single-leaf tests).
    """
    client = get_client()
    skill_text = _load_adjudicator_skill()
    if digest is None:
        digest = build_evidence_digest(case)
    system_blocks = _build_system_blocks(skill_text, digest)

    user = (
        "Evaluate the following policy criterion against the patient case.\n\n"
        "The CASE EVIDENCE DIGEST is already in context above. Use it as your "
        "starting point and drill in with tools only when the digest is "
        "insufficient.\n\n"
        "CRITERION:\n"
        f"{_build_criterion_payload(leaf, policy)}\n\n"
        "When you have enough evidence, return a CriterionVerdict via the "
        "`return_criterionverdict` tool. Do not free-form text the answer."
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
        system=system_blocks,
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
    leaf_budget_seconds: float = 120.0,
    policy: Policy | None = None,
) -> FullAdjudication:
    """Adjudicate every leaf in the tree and every exclusion in parallel.

    If `branch` is "initial" or "repeat", we filter the tree to only the
    leaves under the corresponding indication subtree (plus eligibility,
    severity, frequency, etc. — anything not under indication.X is shared).

    Each leaf runs under `leaf_budget_seconds`. A leaf that exceeds its
    budget (or raises) is recorded as an `unclear` verdict with an
    appropriate `missing_info` note — the batch as a whole still completes
    and the determination layer can act on the partial results.
    """
    leaves = list(iter_leaves(criteria_root))
    if branch:
        leaves = _filter_leaves_for_branch(leaves, branch)

    exclusions = list(exclusions)

    # B: deterministic short-circuit pass. Any leaf whose `evaluator_kind` is
    # structurally checkable (ICD pattern, age threshold, dictionary class
    # membership) is resolved here without an LLM call. Ambiguous results
    # return None and fall through to the LLM, so this is purely additive —
    # adding handlers can only ever steal work from the LLM, never produce a
    # wrong verdict the LLM would have gotten right.
    leaf_pre: dict[str, AdjudicationResult] = {}
    excl_pre: dict[str, AdjudicationResult] = {}
    leaves_remaining: list[CriterionLeaf] = []
    excl_remaining: list[Exclusion] = []
    for l in leaves:
        v = try_deterministic_verdict(l, case)
        if v is not None:
            leaf_pre[l.id] = AdjudicationResult(
                verdict=v, iterations=0, usage=Usage(), tool_calls=[],
            )
        else:
            leaves_remaining.append(l)
    for ex in exclusions:
        v = try_deterministic_verdict(ex, case)
        if v is not None:
            excl_pre[ex.id] = AdjudicationResult(
                verdict=v, iterations=0, usage=Usage(), tool_calls=[],
            )
        else:
            excl_remaining.append(ex)

    log.info(
        "adjudicator.start",
        leaf_count=len(leaves), exclusion_count=len(exclusions),
        deterministic_leaves=len(leaf_pre), deterministic_exclusions=len(excl_pre),
        llm_leaves=len(leaves_remaining), llm_exclusions=len(excl_remaining),
        branch=branch, parallel=parallel, leaf_budget_s=leaf_budget_seconds,
    )

    # Build the per-case digest once; reused (and prompt-cached) for every leaf.
    digest = build_evidence_digest(case) if (leaves_remaining or excl_remaining) else ""

    semaphore = asyncio.Semaphore(parallel)

    async def _run(node: CriterionLeaf | Exclusion) -> tuple[str, AdjudicationResult]:
        async with semaphore:
            try:
                r = await asyncio.wait_for(
                    adjudicate_criterion(node, case, digest=digest, policy=policy),
                    timeout=leaf_budget_seconds,
                )
                return node.id, r
            except asyncio.TimeoutError:
                log.warning(
                    "adjudicator.leaf_timeout",
                    criterion_id=node.id,
                    budget_s=leaf_budget_seconds,
                )
                return node.id, _fallback_result(
                    node.id,
                    f"adjudicator exceeded per-leaf budget of {leaf_budget_seconds:.0f}s",
                )
            except Exception as e:
                log.error("adjudicator.leaf_error", criterion_id=node.id, error=str(e))
                return node.id, _fallback_result(
                    node.id, f"adjudicator raised: {e!s}"
                )

    leaf_tasks = [asyncio.create_task(_run(l)) for l in leaves_remaining]
    excl_tasks = [asyncio.create_task(_run(e)) for e in excl_remaining]
    leaf_results = await asyncio.gather(*leaf_tasks)
    excl_results = await asyncio.gather(*excl_tasks)

    leaf_verdicts: dict[str, CriterionVerdict] = {}
    excl_verdicts: dict[str, CriterionVerdict] = {}
    iterations: dict[str, int] = {}
    total_usage = Usage()

    # Start with the deterministic short-circuit results, then layer LLM results.
    for nid, r in leaf_pre.items():
        leaf_verdicts[nid] = r.verdict
        iterations[nid] = r.iterations
    for nid, r in excl_pre.items():
        excl_verdicts[nid] = r.verdict
        iterations[nid] = r.iterations

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
        deterministic_leaves=len(leaf_pre),
        deterministic_exclusions=len(excl_pre),
        escalations=len(case.escalations),
        total_tokens_in=total_usage.input_tokens,
        total_tokens_out=total_usage.output_tokens,
        cache_read=total_usage.cache_read_tokens,
        cache_creation=total_usage.cache_creation_tokens,
        cache_hit_pct=round(total_usage.cache_hit_rate * 100, 1),
        cost_usd=round(total_usage.cost_usd, 4),
    )

    return FullAdjudication(
        leaf_verdicts=leaf_verdicts,
        exclusion_verdicts=excl_verdicts,
        escalations=list(case.escalations),
        total_usage=total_usage,
        iterations=iterations,
    )


def _fallback_result(criterion_id: str, reason: str) -> AdjudicationResult:
    """Build an `unclear` AdjudicationResult for a leaf that timed out or
    raised. Used so one stuck leaf can't drop the whole batch."""
    return AdjudicationResult(
        verdict=CriterionVerdict(
            criterion_id=criterion_id,
            verdict="unclear",
            confidence=0.2,
            reasoning=reason,
            missing_info=[reason],
        ),
        iterations=0,
        usage=Usage(),
        tool_calls=[],
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
