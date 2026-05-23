"""Doctor-facing submission endpoint.

POST /v1/doctor/submit (multipart) accepts:
  - pdf: the patient's clinical PDF
  - metadata: JSON-encoded SubmissionData fields (everything except pdf_bytes)

It builds a Da Vinci PAS Bundle, creates a Case row with
processing_stage="received", kicks off the pipeline in a background asyncio
task, and returns {case_id, bundle_preview} immediately.

The background task updates Case.processing_stage between pipeline steps so
the doctor's Streamlit UI can poll GET /v1/cases/{case_id} every 2s and
render live progress.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.api.cases import _persist, _update_case_stage
from app.db.engine import session_scope
from app.models import Case
from app.orchestrator import evaluate_pa_case
from app.pas.bundle_constructor import ICD10Code, SubmissionData, build_pas_bundle


log = structlog.get_logger()

router = APIRouter(prefix="/v1/doctor", tags=["doctor"])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/submit")
async def doctor_submit(
    pdf: UploadFile = File(...),
    metadata: str = Form(...),
) -> dict[str, Any]:
    """Build a PAS Bundle from the upload + form, fire the pipeline in the
    background, and return the case_id + Bundle preview immediately."""
    # Parse and validate the metadata payload
    try:
        data = json.loads(metadata)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"metadata must be valid JSON: {e}") from e

    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(400, "pdf upload is empty")

    try:
        submission = _build_submission_from_payload(data, pdf_bytes, pdf.filename or "upload.pdf")
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"metadata missing or invalid: {e}") from e

    case_id = data.get("case_id") or f"doc-{uuid.uuid4().hex[:10]}"
    bundle, _ = build_pas_bundle(submission, case_id=case_id)

    # Create the Case row immediately so the polling UI sees it on the first tick
    await _create_pending_case(case_id, submission, bundle)

    # Fire the pipeline in the background
    asyncio.create_task(_run_pipeline_async(case_id, bundle))

    return {
        "case_id": case_id,
        "processing_stage": "received",
        "bundle_entry_count": len(bundle["entry"]),
        "bundle_size_bytes": _approx_bundle_size(bundle),
        "bundle_preview": bundle,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_submission_from_payload(
    data: dict, pdf_bytes: bytes, pdf_filename: str
) -> SubmissionData:
    icd10_raw = data.get("icd10_codes") or []
    icd10s: list[ICD10Code] = []
    for entry in icd10_raw:
        if isinstance(entry, str):
            icd10s.append(ICD10Code(code=entry, display=entry, kind="secondary"))
        else:
            icd10s.append(ICD10Code(
                code=entry["code"],
                display=entry.get("display", entry["code"]),
                kind=entry.get("kind", "secondary"),
            ))
    if not icd10s:
        raise ValueError("at least one icd10 code is required")
    # Ensure exactly one primary
    primaries = [c for c in icd10s if c.kind == "primary"]
    if not primaries:
        icd10s[0].kind = "primary"

    return SubmissionData(
        patient_given=data["patient_given"],
        patient_family=data["patient_family"],
        patient_dob=data["patient_dob"],
        patient_gender=data.get("patient_gender", "unknown"),
        patient_state=data["patient_state"],
        payer_id=data["payer_id"],
        payer_display=data.get("payer_display", data["payer_id"].title()),
        member_id=data["member_id"],
        line_of_business=data["line_of_business"],
        plan_name=data.get("plan_name", data["payer_id"]),
        cpt_code=data["cpt_code"],
        cpt_display=data.get("cpt_display", data["cpt_code"]),
        service_date=data["service_date"],
        icd10_codes=icd10s,
        body_site_display=data.get("body_site_display", ""),
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_filename,
        provider_org_name=data.get("provider_org_name", "Submitting Provider"),
        practitioner_family=data.get("practitioner_family", "Provider"),
        practitioner_given=data.get("practitioner_given", "Doctor"),
    )


async def _create_pending_case(
    case_id: str, submission: SubmissionData, bundle: dict
) -> None:
    """Insert the Case row in `received` state so the polling UI has something
    to render immediately. Subsequent stage updates mutate this row in place."""
    async with session_scope() as session:
        existing = await session.get(Case, case_id)
        patient_display = f"{submission.patient_given} {submission.patient_family}".strip()
        if existing:
            existing.status = "processing"
            existing.processing_stage = "received"
            existing.error_message = None
            existing.patient_display = patient_display
            existing.cpt_code = submission.cpt_code
            existing.payer_id = submission.payer_id
            existing.inbound_bundle = bundle
            return
        case = Case(
            id=case_id,
            status="processing",
            processing_stage="received",
            patient_display=patient_display,
            cpt_code=submission.cpt_code,
            payer_id=submission.payer_id,
            inbound_bundle=bundle,
        )
        session.add(case)


async def _run_pipeline_async(case_id: str, bundle: dict) -> None:
    """Background task: runs the full orchestrator and persists the result.
    Writes stage updates between pipeline steps so the doctor UI sees progress."""

    async def stage_cb(stage: str) -> None:
        await _update_case_stage(case_id, stage)

    try:
        run = await evaluate_pa_case(
            bundle,
            run_intake_on_documents=True,
            case_id=case_id,
            progress_callback=stage_cb,
        )
        await _persist(run)
        # _persist already sets processing_stage="complete", but in case of an
        # early no-match return we set it here as a belt-and-suspenders.
        await _update_case_stage(case_id, "complete")
        log.info("doctor.pipeline_complete", case_id=case_id,
                 outcome=run.determination.outcome if run.determination else None)
    except Exception as e:
        log.exception("doctor.pipeline_failed", case_id=case_id, error=str(e))
        await _update_case_stage(case_id, "failed", error=str(e)[:1500])


def _approx_bundle_size(bundle: dict) -> int:
    return len(json.dumps(bundle))
