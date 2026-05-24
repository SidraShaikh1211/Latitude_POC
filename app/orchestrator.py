"""End-to-end PA orchestrator.

One function — `evaluate_pa_case` — takes an inbound Da Vinci PAS Bundle and
produces a fully-resolved case (selection + adjudication + decision +
reviewer narrative + outbound ClaimResponse Bundle). Shared by:
  - REST API:  POST /fhir/Claim/$submit
  - MCP tool:  evaluate_prior_auth
  - Seed script: scripts/seed_smith_case.py
"""

from __future__ import annotations

import base64
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog


ProgressCallback = Callable[[str], Awaitable[None]]
# Partial-result callback: fires after each pipeline step with the fields
# now known. Lets the caller persist results incrementally so a UI polling
# the case row sees data appear progressively instead of all at the end.
PartialCallback = Callable[[dict[str, Any]], Awaitable[None]]

from app.determination.decider import Determination, decide
from app.determination.reviewer import ReviewerOutput, ReviewResult, review_case
from app.determination.rollup import NodeVerdict, rollup
from app.extraction.intake import IntakeResult, run_intake
from app.extraction.pdf import ExtractedDocument, extract_pdf
from app.llm.client import Usage
from app.pas.bundle_builder import BuiltResponse, build_pas_response_bundle
from app.pas.bundle_parser import ParsedBundle, parse_pas_bundle
from app.policy.adjudicator import (
    CriterionVerdict,
    FullAdjudication,
    adjudicate_all,
)
from app.policy.registry import PolicyRegistry, get_registry
from app.policy.selector import SelectionResult, select_policy
from app.policy.tools import CaseFacts


log = structlog.get_logger()


def _stage_metrics(name: str, duration_s: float, usage: Usage, llm_calls: int) -> dict[str, Any]:
    """Format a single pipeline-stage entry for the metrics blob."""
    return {
        "name": name,
        "duration_seconds": round(duration_s, 3),
        "tokens_in": usage.input_tokens,
        "tokens_out": usage.output_tokens,
        "cache_read": usage.cache_read_tokens,
        "cache_creation": usage.cache_creation_tokens,
        "cost_usd": round(usage.cost_usd, 6),
        "llm_calls": llm_calls,
    }


def _build_metrics(
    *,
    started_at: float,
    stages: list[dict[str, Any]],
    adjudication_leaves: list[dict[str, Any]],
) -> dict[str, Any]:
    """Roll the per-stage entries up into a single metrics blob."""
    totals_usage = Usage()
    total_calls = 0
    for s in stages:
        totals_usage.input_tokens += s["tokens_in"]
        totals_usage.output_tokens += s["tokens_out"]
        totals_usage.cache_read_tokens += s["cache_read"]
        totals_usage.cache_creation_tokens += s["cache_creation"]
        totals_usage.cost_usd += s["cost_usd"]
        total_calls += s["llm_calls"]
    return {
        "duration_seconds": round(time.perf_counter() - started_at, 3),
        "stages": stages,
        "adjudication_leaves": adjudication_leaves,
        "totals": {
            "tokens_in": totals_usage.input_tokens,
            "tokens_out": totals_usage.output_tokens,
            "cache_read": totals_usage.cache_read_tokens,
            "cache_creation": totals_usage.cache_creation_tokens,
            "cost_usd": round(totals_usage.cost_usd, 6),
            "llm_calls": total_calls,
            "cache_hit_rate": round(totals_usage.cache_hit_rate, 4),
        },
    }


@dataclass
class CaseRun:
    case_id: str
    parsed_in: ParsedBundle
    selection: SelectionResult
    intake: IntakeResult | None
    adjudication: FullAdjudication | None
    rollup_root: NodeVerdict | None
    determination: Determination | None
    reviewer: ReviewResult | None
    response: BuiltResponse | None
    error: str | None = None
    # Per-stage performance metrics. Populated whether the caller wired
    # on_partial (async path) or not (sync wait=true path); _persist reads
    # this and writes Case.metrics.
    metrics: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "selection_status": self.selection.status,
            "selected_policy_id": self.selection.selected_policy_id,
            "branch": self.selection.branch,
            "outcome": self.determination.outcome if self.determination else None,
            "narrative": self.reviewer.output.narrative if self.reviewer else None,
            "missing_info_count": (
                len(self.reviewer.output.missing_info) if self.reviewer else 0
            ),
            "claim_response_id": self.response.claim_response_id if self.response else None,
            "error": self.error,
        }


async def evaluate_pa_case(
    bundle_data: dict,
    *,
    run_intake_on_documents: bool = True,
    registry: PolicyRegistry | None = None,
    case_id: str | None = None,
    progress_callback: ProgressCallback | None = None,
    on_partial: PartialCallback | None = None,
) -> CaseRun:
    """Run the full pipeline on an inbound PAS Bundle.

    Steps:
      1. Parse the inbound Bundle → CaseContext + FactCollection
      2. Materialize ExtractedDocument(s) from embedded Binary PDFs (if any)
      3. Optionally run Intake on each PDF (Claude structured-output)
      4. Select policy (deterministic; reads prior Procedures from Bundle)
      5. Adjudicate every leaf + exclusion in parallel (LLM)
      6. Roll up the leaf verdicts → root verdict
      7. Apply Decider (exclusion short-circuit + outcome mapping)
      8. Reviewer agent → narrative + refined missing-info
      9. Build outbound PAS ClaimResponse Bundle

    Callbacks:
      progress_callback(stage)  — fires at each stage boundary so callers can
                                  write Case.processing_stage to the DB.
      on_partial(fields)        — fires after each step finishes producing
                                  results. `fields` is a dict of Case columns
                                  that are now known. Lets a polling UI see
                                  data appear progressively.
    """
    case_id = case_id or f"case-{uuid.uuid4().hex[:10]}"
    log.info("orchestrator.start", case_id=case_id)

    # Per-run perf telemetry — appended-to as each stage finishes, then
    # persisted onto Case.metrics via on_partial. Surfaced by the
    # Performance page (one row per run, expand for per-stage breakdown).
    run_started = time.perf_counter()
    stages: list[dict[str, Any]] = []
    adjudication_leaves: list[dict[str, Any]] = []

    async def _step(stage: str) -> None:
        if progress_callback is not None:
            try:
                await progress_callback(stage)
            except Exception as e:
                log.warning("orchestrator.progress_callback_failed", stage=stage, error=str(e))

    async def _emit(partial: dict[str, Any]) -> None:
        if on_partial is not None and partial:
            try:
                await on_partial(partial)
            except Exception as e:
                log.warning("orchestrator.on_partial_failed", error=str(e))

    async def _emit_metrics() -> None:
        """Emit the current snapshot of the metrics blob so the row is up to
        date even if a downstream stage fails before we reach the bottom of
        the pipeline."""
        await _emit({
            "metrics": _build_metrics(
                started_at=run_started,
                stages=list(stages),
                adjudication_leaves=list(adjudication_leaves),
            ),
        })

    await _step("parsing")
    parsed = parse_pas_bundle(bundle_data)

    # Materialize documents
    documents = _documents_from_bundle(parsed)
    log.info("orchestrator.documents", count=len(documents), ids=list(documents.keys()))

    # Build CaseFacts shell now; intake will fill `extracted`
    case_facts = CaseFacts(
        bundle_facts=parsed.facts,
        documents=documents,
        service_date=parsed.context.service_date,
    )

    # Optional: run intake on PDFs
    intake: IntakeResult | None = None
    if run_intake_on_documents and documents:
        await _step("intake")
        # One DocumentReference + Binary per case in the prototype. For
        # multi-doc cases we'd merge ExtractedFacts; keep scope to first doc.
        first_doc = next(iter(documents.values()))
        intake_start = time.perf_counter()
        try:
            intake = await run_intake(first_doc)
            case_facts.extracted = intake.facts
            log.info(
                "orchestrator.intake_done",
                citations_passed=intake.citation_passed,
                citations_failed=intake.citation_failed,
            )
            stages.append(_stage_metrics(
                "intake",
                time.perf_counter() - intake_start,
                intake.usage,
                llm_calls=1,
            ))
            await _emit({"extracted_facts": intake.facts.model_dump()})
            await _emit_metrics()
        except Exception as e:
            log.error("orchestrator.intake_failed", error=str(e))

    # Select policy
    await _step("selecting")
    reg = registry or get_registry()
    selection = select_policy(
        parsed.context,
        registry=reg,
        prior_procedures=parsed.facts.procedures_prior,
        case_facts=case_facts,
    )
    log.info("orchestrator.selection", status=selection.status,
             selected=selection.selected_policy_id, branch=selection.branch)

    await _emit({
        "selected_policy_id": selection.selected_policy_id,
        "branch": selection.branch,
        "policy_selection": {
            "status": selection.status,
            "selected_policy_id": selection.selected_policy_id,
            "branch": selection.branch,
            "selection_reason": selection.selection_reason,
            "eliminated": selection.eliminated,
        },
    })

    if selection.status != "ok" or selection.selected_policy_id is None:
        # Build an error-style ClaimResponse Bundle without adjudication
        return _build_no_match_response(
            case_id=case_id, parsed=parsed, selection=selection, intake=intake
        )

    policy = reg.get(selection.selected_policy_id)
    assert policy is not None

    # Adjudicate
    await _step("adjudicating")
    adj_start = time.perf_counter()
    adjudication = await adjudicate_all(
        criteria_root=policy.criteria,
        exclusions=policy.exclusions,
        case=case_facts,
        branch=selection.branch,
        policy=policy,
    )

    # Per-leaf breakdown — used by the Performance page's expand row. Each
    # entry carries the verdict, iteration count (== number of agent-loop
    # LLM calls; 0 means resolved by the deterministic short-circuit), and
    # the leaf's own Usage. Excludes (ID prefixed "X") and leaves share one
    # list so the UI just sorts by cost.
    adj_llm_calls = 0
    for nid, iters in adjudication.iterations.items():
        u = adjudication.leaf_usages.get(nid, Usage())
        verdict = (
            adjudication.leaf_verdicts.get(nid)
            or adjudication.exclusion_verdicts.get(nid)
        )
        adjudication_leaves.append({
            "criterion_id": nid,
            "kind": "exclusion" if nid in adjudication.exclusion_verdicts else "leaf",
            "verdict": verdict.verdict if verdict else None,
            "iterations": iters,
            "tokens_in": u.input_tokens,
            "tokens_out": u.output_tokens,
            "cache_read": u.cache_read_tokens,
            "cache_creation": u.cache_creation_tokens,
            "cost_usd": round(u.cost_usd, 6),
        })
        adj_llm_calls += iters
    stages.append(_stage_metrics(
        "adjudication",
        time.perf_counter() - adj_start,
        adjudication.total_usage,
        llm_calls=adj_llm_calls,
    ))

    await _emit({
        "criteria_evaluation": {
            "leaf_verdicts": {
                cid: v.model_dump() for cid, v in adjudication.leaf_verdicts.items()
            },
            "exclusion_verdicts": {
                eid: v.model_dump() for eid, v in adjudication.exclusion_verdicts.items()
            },
        },
    })
    await _emit_metrics()

    # Roll up
    leaf_verdict_strs = {cid: v.verdict for cid, v in adjudication.leaf_verdicts.items()}
    rolled = rollup(policy.criteria, leaf_verdict_strs)

    # Decide
    determination = decide(
        root_verdict=rolled.verdict,
        exclusion_verdicts={
            eid: v.verdict for eid, v in adjudication.exclusion_verdicts.items()
        },
        escalations=adjudication.escalations,
    )

    # Reviewer
    await _step("reviewing")
    rev_start = time.perf_counter()
    reviewer = await review_case(
        policy=policy,
        determination=determination,
        leaf_verdicts=adjudication.leaf_verdicts,
        exclusion_verdicts=adjudication.exclusion_verdicts,
        case=case_facts,
    )
    stages.append(_stage_metrics(
        "reviewer",
        time.perf_counter() - rev_start,
        reviewer.usage,
        llm_calls=reviewer.iterations,
    ))

    await _emit({
        "outcome": determination.outcome,
        "determination": {
            "outcome": determination.outcome,
            "rationale": determination.rationale,
            "triggered_exclusions": determination.triggered_exclusions,
            "escalation_reasons": determination.escalation_reasons,
            "narrative": reviewer.output.narrative,
            "missing_info": [mi.model_dump() for mi in reviewer.output.missing_info],
        },
    })

    # Build outbound Bundle
    await _step("building_response")
    response = build_pas_response_bundle(
        parsed_in=parsed,
        policy=policy,
        determination=determination,
        leaf_verdicts=adjudication.leaf_verdicts,
        exclusion_verdicts=adjudication.exclusion_verdicts,
        reviewer=reviewer.output,
        case_id=case_id,
    )

    await _emit({"pas_response_bundle": response.bundle})

    final_metrics = _build_metrics(
        started_at=run_started,
        stages=list(stages),
        adjudication_leaves=list(adjudication_leaves),
    )
    await _emit({"metrics": final_metrics})

    return CaseRun(
        case_id=case_id,
        parsed_in=parsed,
        selection=selection,
        intake=intake,
        adjudication=adjudication,
        rollup_root=rolled,
        determination=determination,
        reviewer=reviewer,
        response=response,
        metrics=final_metrics,
    )


def _documents_from_bundle(parsed: ParsedBundle) -> dict[str, ExtractedDocument]:
    """Decode every Binary PDF in the Bundle and return ExtractedDocuments keyed
    by DocumentReference.id (preferred) or Binary.id (fallback)."""
    import os
    import tempfile

    docs: dict[str, ExtractedDocument] = {}
    binaries = parsed.facts.binaries
    doc_refs = parsed.facts.documents

    # Build (doc_ref_id, binary_id) pairs by inspecting DocumentReference.content[].attachment.url
    pairs: list[tuple[str, str]] = []
    for dr in doc_refs:
        dr_id = dr.get("id") or ""
        for c in dr.get("content") or []:
            url = (c.get("attachment") or {}).get("url") or ""
            if url.startswith("Binary/"):
                bid = url.removeprefix("Binary/")
                if bid in binaries:
                    pairs.append((dr_id, bid))

    # Fallback: pair any unmatched binaries to themselves
    used = {bid for _, bid in pairs}
    for bid in binaries:
        if bid not in used:
            pairs.append((bid, bid))

    for doc_id, bid in pairs:
        pdf_bytes = binaries[bid]
        # extract_pdf takes a path; stage through a temp file, then unlink so
        # we don't leak one PDF per case into /tmp for the process lifetime.
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name
        try:
            doc = extract_pdf(tmp_path, document_id=doc_id)
            docs[doc_id] = doc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    return docs


def _build_no_match_response(
    *, case_id: str, parsed: ParsedBundle, selection: SelectionResult, intake: IntakeResult | None
) -> CaseRun:
    from datetime import datetime, timezone

    claim_response = {
        "resourceType": "ClaimResponse",
        "id": f"claim-response-{case_id}",
        "status": "active",
        "type": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/claim-type",
                             "code": "professional"}]},
        "use": "preauthorization",
        "created": datetime.now(timezone.utc).isoformat(),
        "outcome": "error",
        "disposition": (
            f"No applicable policy found for this submission. "
            f"selector_status={selection.status}, reason: {selection.selection_reason}"
        ),
        "insurer": {"display": parsed.context.payer_id or "unknown"},
        "processNote": [
            {"number": i + 1, "type": "print",
             "text": f"[ELIMINATED {pid}] {reason}"}
            for i, (pid, reason) in enumerate(selection.eliminated[:20])
        ],
    }
    bundle = {
        "resourceType": "Bundle",
        "id": f"pas-response-{case_id}",
        "type": "collection",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry": [{"fullUrl": f"urn:uuid:{claim_response['id']}", "resource": claim_response}],
    }
    response = BuiltResponse(
        bundle=bundle,
        claim_response_id=claim_response["id"],
        pre_auth_ref=None,
        process_note_count=len(claim_response["processNote"]),
    )
    return CaseRun(
        case_id=case_id, parsed_in=parsed, selection=selection,
        intake=intake, adjudication=None, rollup_root=None,
        determination=Determination(
            outcome="needs_human_review",
            rationale=selection.selection_reason,
            triggered_exclusions=[], escalation_reasons=[],
        ),
        reviewer=None, response=response,
        error=None if selection.status == "no_match" else selection.selection_reason,
    )
