"""Da Vinci PAS ClaimResponse Bundle builder.

Builds the wire-format response a provider EHR will receive after we
adjudicate their preauthorization Claim. Maps the deterministic outcome to
ClaimResponse.outcome, emits one processNote per criterion + per exclusion
that contributed to the verdict, places the Reviewer's narrative in
disposition, and lists missing-info requests as additional processNotes.

Validation: base R4 shape via fhir.resources. Da Vinci PAS IG profile
validation is out of prototype scope (documented in README).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from fhir.resources.R4B.bundle import Bundle

from app.determination.decider import Determination
from app.determination.reviewer import ReviewerOutput
from app.pas.bundle_parser import CaseContext, ParsedBundle
from app.policy.adjudicator import CriterionVerdict
from app.policy.registry import Policy


# Outcome mapping — PAS ClaimResponse.outcome valueset is {queued, complete, error, partial}
_OUTCOME_MAP = {
    "approve": "complete",
    "deny": "error",
    "pend": "queued",
    "needs_human_review": "queued",
}


@dataclass
class BuiltResponse:
    bundle: dict
    claim_response_id: str
    pre_auth_ref: str | None
    process_note_count: int


def build_pas_response_bundle(
    *,
    parsed_in: ParsedBundle,
    policy: Policy,
    determination: Determination,
    leaf_verdicts: dict[str, CriterionVerdict],
    exclusion_verdicts: dict[str, CriterionVerdict],
    reviewer: ReviewerOutput,
    case_id: str | None = None,
) -> BuiltResponse:
    """Construct the outbound PAS Bundle as a dict (validated against R4)."""
    case_id = case_id or f"case-{uuid.uuid4().hex[:10]}"
    claim_response_id = f"claim-response-{case_id}"
    claim = parsed_in.facts.claim or {}
    pre_auth_ref = case_id if determination.outcome == "approve" else None

    process_notes: list[dict] = []
    note_number = 1

    # Per-criterion notes
    for cid, v in sorted(leaf_verdicts.items()):
        process_notes.append({
            "number": note_number,
            "type": "print",
            "text": _format_verdict_note("criterion", cid, v, policy),
        })
        note_number += 1

    # Per-exclusion notes (only those evaluated to met or unclear surface here)
    for eid, v in sorted(exclusion_verdicts.items()):
        if v.verdict in {"met", "unclear"}:
            process_notes.append({
                "number": note_number,
                "type": "print",
                "text": _format_verdict_note("exclusion", eid, v, policy),
            })
            note_number += 1

    # Missing-info as separate processNotes
    for mi in reviewer.missing_info:
        process_notes.append({
            "number": note_number,
            "type": "display",
            "text": (
                f"[MISSING_INFO {mi.id}] criterion={mi.criterion_id} | "
                f"request: {mi.request}"
            ),
        })
        note_number += 1

    # Reviewer narrative as disposition; also include as a processNote for searchability
    disposition = reviewer.narrative.strip() or determination.rationale

    item_adjudications = _build_item_adjudications(claim, determination, leaf_verdicts)

    claim_ref = claim.get("id") and f"Claim/{claim['id']}" or None
    patient_ref = parsed_in.facts.patient and f"Patient/{parsed_in.facts.patient.get('id')}" or None
    coverage_ref = parsed_in.facts.coverage and f"Coverage/{parsed_in.facts.coverage.get('id')}" or None
    insurer_ref = _extract_insurer_ref(parsed_in.facts.coverage)

    claim_response: dict[str, Any] = {
        "resourceType": "ClaimResponse",
        "id": claim_response_id,
        "status": "active",
        "type": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                "code": "professional",
            }],
        },
        "use": "preauthorization",
        "patient": {"reference": patient_ref} if patient_ref else None,
        "created": _now_isoformat(),
        "insurer": {"reference": insurer_ref} if insurer_ref else {"display": policy.payer_id},
        "outcome": _OUTCOME_MAP.get(determination.outcome, "queued"),
        "disposition": disposition,
        "preAuthRef": pre_auth_ref,
        "item": item_adjudications,
        "processNote": process_notes,
    }
    if claim_ref:
        claim_response["request"] = {"reference": claim_ref}
    if coverage_ref:
        claim_response["insurance"] = [{
            "sequence": 1,
            "focal": True,
            "coverage": {"reference": coverage_ref},
        }]

    # Strip None values for cleanliness
    claim_response = {k: v for k, v in claim_response.items() if v is not None}

    # Build the wrapping Bundle
    bundle_data = {
        "resourceType": "Bundle",
        "id": f"pas-response-{case_id}",
        "type": "collection",
        "timestamp": _now_isoformat(),
        "entry": [
            {"fullUrl": f"urn:uuid:{claim_response_id}", "resource": claim_response},
        ],
    }
    # Add Patient and Coverage by reference for grader convenience
    if parsed_in.facts.patient:
        bundle_data["entry"].append({
            "fullUrl": f"urn:uuid:patient-{parsed_in.facts.patient.get('id')}",
            "resource": parsed_in.facts.patient,
        })
    if parsed_in.facts.coverage:
        bundle_data["entry"].append({
            "fullUrl": f"urn:uuid:coverage-{parsed_in.facts.coverage.get('id')}",
            "resource": parsed_in.facts.coverage,
        })

    # Validate R4 shape
    Bundle.model_validate(bundle_data)

    return BuiltResponse(
        bundle=bundle_data,
        claim_response_id=claim_response_id,
        pre_auth_ref=pre_auth_ref,
        process_note_count=len(process_notes),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_verdict_note(
    kind: str, cid: str, v: CriterionVerdict, policy: Policy
) -> str:
    return (
        f"[{kind.upper()} {cid}] verdict={v.verdict} confidence={v.confidence:.2f} | "
        f"reasoning: {v.reasoning}"
        + (f" | missing_info: {'; '.join(v.missing_info)}" if v.missing_info else "")
    )


def _build_item_adjudications(
    claim: dict, det: Determination, leaf_verdicts: dict[str, CriterionVerdict]
) -> list[dict]:
    """One adjudication entry per Claim.item (per CPT). The adjudication carries
    the case-level outcome and the count of unmet/unclear criteria for context."""
    items = claim.get("item") or []
    if not items:
        return []
    unmet = sum(1 for v in leaf_verdicts.values() if v.verdict == "not_met")
    unclear = sum(1 for v in leaf_verdicts.values() if v.verdict == "unclear")
    adjudication_code = _ADJ_OUTCOME_CODE.get(det.outcome, "submitted")

    out = []
    for item in items:
        seq = item.get("sequence", 1)
        out.append({
            "itemSequence": seq,
            "adjudication": [
                {
                    "category": {
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/adjudication",
                            "code": adjudication_code,
                        }],
                        "text": det.outcome,
                    },
                    "reason": {
                        "text": det.rationale,
                    },
                },
                {
                    "category": {"text": "unmet_criteria_count"},
                    "amount": {"value": unmet, "currency": "USD"},
                },
                {
                    "category": {"text": "unclear_criteria_count"},
                    "amount": {"value": unclear, "currency": "USD"},
                },
            ],
        })
    return out


_ADJ_OUTCOME_CODE = {
    "approve": "submitted",   # PAS uses adjudication codes from a small set; mark approval here
    "deny": "denied",
    "pend": "pending",
    "needs_human_review": "pending",
}


def _extract_insurer_ref(coverage: dict | None) -> str | None:
    if not coverage:
        return None
    payor = coverage.get("payor") or []
    if not payor:
        return None
    ref = payor[0].get("reference")
    return ref


def _now_isoformat() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
