"""End-to-end PA orchestrator.

One function — `evaluate_pa_case` — takes an inbound Da Vinci PAS Bundle and
produces a fully-resolved case (selection + adjudication + decision +
reviewer narrative + outbound ClaimResponse Bundle). Shared by:
  - REST API:  POST /fhir/Claim/$submit
  - MCP tool:  evaluate_prior_auth
  - Streamlit demo seeding
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog


ProgressCallback = Callable[[str], Awaitable[None]]

from app.determination.decider import Determination, decide
from app.determination.reviewer import ReviewerOutput, ReviewResult, review_case
from app.determination.rollup import NodeVerdict, rollup
from app.extraction.intake import IntakeResult, run_intake
from app.extraction.pdf import ExtractedDocument, extract_pdf
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

    If `progress_callback` is provided, it is invoked at each of the 6
    user-visible stage boundaries (parsing, intake, selecting, adjudicating,
    reviewing, building_response). The callback is async and should be
    cheap — typically used to write Case.processing_stage to the DB.
    """
    case_id = case_id or f"case-{uuid.uuid4().hex[:10]}"
    log.info("orchestrator.start", case_id=case_id)

    async def _step(stage: str) -> None:
        if progress_callback is not None:
            try:
                await progress_callback(stage)
            except Exception as e:
                log.warning("orchestrator.progress_callback_failed", stage=stage, error=str(e))

    await _step("parsing")
    parsed = parse_pas_bundle(bundle_data)

    # Materialize documents
    documents = _documents_from_bundle(parsed)
    log.info("orchestrator.documents", count=len(documents), ids=list(documents.keys()))

    # Build CaseFacts shell now; intake will fill `extracted`
    case_facts = CaseFacts(bundle_facts=parsed.facts, documents=documents)

    # Optional: run intake on PDFs
    intake: IntakeResult | None = None
    if run_intake_on_documents and documents:
        await _step("intake")
        # Smith case has one DocumentReference + one Binary; intake one doc.
        # For multi-doc cases we'd merge ExtractedFacts; keep the prototype scope to first doc.
        first_doc = next(iter(documents.values()))
        try:
            intake = await run_intake(first_doc)
            case_facts.extracted = intake.facts
            log.info(
                "orchestrator.intake_done",
                citations_passed=intake.citation_passed,
                citations_failed=intake.citation_failed,
            )
        except Exception as e:
            log.error("orchestrator.intake_failed", error=str(e))

    # Select policy
    await _step("selecting")
    reg = registry or get_registry()
    selection = select_policy(
        parsed.context, registry=reg, prior_procedures=parsed.facts.procedures_prior
    )
    log.info("orchestrator.selection", status=selection.status,
             selected=selection.selected_policy_id, branch=selection.branch)

    if selection.status != "ok" or selection.selected_policy_id is None:
        # Build an error-style ClaimResponse Bundle without adjudication
        return _build_no_match_response(
            case_id=case_id, parsed=parsed, selection=selection, intake=intake
        )

    policy = reg.get(selection.selected_policy_id)
    assert policy is not None

    # Adjudicate
    await _step("adjudicating")
    adjudication = await adjudicate_all(
        criteria_root=policy.criteria,
        exclusions=policy.exclusions,
        case=case_facts,
        branch=selection.branch,
    )

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
    reviewer = await review_case(
        policy=policy,
        determination=determination,
        leaf_verdicts=adjudication.leaf_verdicts,
        exclusion_verdicts=adjudication.exclusion_verdicts,
        case=case_facts,
    )

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
    )


def _documents_from_bundle(parsed: ParsedBundle) -> dict[str, ExtractedDocument]:
    """Decode every Binary PDF in the Bundle and return ExtractedDocuments keyed
    by DocumentReference.id (preferred) or Binary.id (fallback)."""
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
        # extract_pdf takes a path; write to a temp file
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name
        doc = extract_pdf(tmp_path, document_id=doc_id)
        docs[doc_id] = doc

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
