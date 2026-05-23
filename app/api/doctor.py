"""Doctor-facing submission endpoint — PDF-only flow.

POST /v1/doctor/submit accepts JUST the PDF:
  - pdf: UploadFile (the patient's clinical PDF; that's the only input)

Pipeline:
  1. Read PDF bytes, extract per-page text (PyMuPDF, deterministic)
  2. Run the metadata extractor (Claude) → SubmissionData (CPT, ICD-10, payer,
     member ID, DOB inferred from clinical content)
  3. Assemble Da Vinci PAS Bundle from the extracted SubmissionData
  4. Persist Case w/ processing_stage="received"
  5. Fire orchestrator in background (intake → selector → adjudicator → reviewer)
  6. Return {case_id, extracted_metadata, bundle_preview} immediately

The doctor doesn't fill any form fields. The metadata extractor does what
an EHR would do automatically: pull patient demographics, insurance info,
and the requested service (with CPT inference from clinical context) out of
the chart.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, File, HTTPException, UploadFile

from app.api.cases import _persist, _update_case_stage
from app.db.engine import session_scope
from app.extraction.metadata import (
    ExtractedMetadata,
    MetadataResult,
    extract_submission_metadata,
    missing_required_fields,
)
from app.extraction.pdf import extract_pdf
from app.models import Case
from app.orchestrator import evaluate_pa_case
from app.pas.bundle_constructor import SubmissionData, build_pas_bundle


log = structlog.get_logger()

router = APIRouter(prefix="/v1/doctor", tags=["doctor"])


# ---------------------------------------------------------------------------
# Endpoint — PDF only
# ---------------------------------------------------------------------------


@router.post("/submit")
async def doctor_submit(pdf: UploadFile = File(...)) -> dict[str, Any]:
    """Submit a clinical PDF for prior authorization.

    The system reads the PDF, infers all needed fields (CPT, ICD-10, patient,
    coverage), assembles the FHIR Bundle, and runs the payer pipeline in the
    background. Returns a case_id immediately; poll GET /v1/cases/{case_id}
    for live status.
    """
    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(400, "pdf upload is empty")
    pdf_filename = pdf.filename or "upload.pdf"

    case_id = f"doc-{uuid.uuid4().hex[:10]}"

    # Persist a minimal Case row in `extracting_metadata` stage so the polling UI
    # sees something instantly while the metadata extractor runs (which takes
    # ~5-15s synchronously below).
    await _create_extracting_metadata_case(case_id, pdf_bytes, pdf_filename)

    # Run the metadata extractor synchronously so we can return the extracted
    # metadata to the doctor's UI as part of the submission response (so they
    # can verify what the system pulled before the slow pipeline starts).
    try:
        meta_result = await _extract_metadata_sync(case_id, pdf_bytes, pdf_filename)
    except Exception as e:
        log.exception("doctor.metadata_extraction_failed", case_id=case_id)
        await _update_case_stage(case_id, "failed", error=f"Metadata extraction failed: {e!s}")
        raise HTTPException(500, f"Could not extract metadata from PDF: {e!s}") from e

    missing = missing_required_fields(meta_result.metadata)
    if missing:
        await _update_case_stage(
            case_id, "failed",
            error=f"Could not determine required fields from PDF: {', '.join(missing)}",
        )
        return {
            "case_id": case_id,
            "processing_stage": "failed",
            "missing_fields": missing,
            "extraction_notes": meta_result.notes,
            "extracted_metadata": meta_result.metadata.model_dump(),
            "error": (
                f"PDF did not contain enough information to assemble a PAS submission. "
                f"Missing: {', '.join(missing)}. The extractor's reasoning is in "
                f"`extraction_notes`. Re-submit with a clearer PDF or a written "
                f"physician order page."
            ),
        }

    # Build the Bundle from extracted metadata
    bundle, _ = build_pas_bundle(meta_result.submission, case_id=case_id)

    # Update the Case row with the now-known fields + Bundle
    await _populate_case_after_extraction(case_id, meta_result.submission, bundle)
    await _update_case_stage(case_id, "received")

    # Fire the payer pipeline in the background
    asyncio.create_task(_run_pipeline_async(case_id, bundle))

    return {
        "case_id": case_id,
        "processing_stage": "received",
        "extracted_metadata": meta_result.metadata.model_dump(),
        "extraction_notes": meta_result.notes,
        "metadata_usage_cost_usd": round(meta_result.usage.cost_usd, 4),
        "bundle_entry_count": len(bundle["entry"]),
        "bundle_size_bytes": len(json.dumps(bundle)),
        "bundle_preview": bundle,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _extract_metadata_sync(
    case_id: str, pdf_bytes: bytes, pdf_filename: str
) -> MetadataResult:
    """Run the metadata extractor synchronously inside the request handler.

    Cost: ~$0.05-0.10 per call. Takes ~5-15 seconds. Done sync so the doctor
    UI can show the extracted metadata immediately on submit response.
    """
    # Write to a temp file so extract_pdf can open it
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        tmp_path = f.name

    doc = extract_pdf(tmp_path, document_id=f"upload-{case_id}")
    log.info("doctor.metadata_extracting", case_id=case_id, pages=doc.page_count)
    return await extract_submission_metadata(
        doc, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename
    )


async def _create_extracting_metadata_case(
    case_id: str, pdf_bytes: bytes, pdf_filename: str
) -> None:
    """Pre-create a Case row with stage='extracting_metadata' so polling sees
    the case instantly (the metadata extractor will block ~5-15s)."""
    async with session_scope() as session:
        case = Case(
            id=case_id,
            status="processing",
            processing_stage="extracting_metadata",
            patient_display=f"(extracting from {pdf_filename})",
        )
        session.add(case)


async def _populate_case_after_extraction(
    case_id: str, submission: SubmissionData, bundle: dict
) -> None:
    """After metadata extraction succeeds, fill in patient/cpt/payer + Bundle."""
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if not case:
            return
        case.patient_display = f"{submission.patient_given} {submission.patient_family}".strip()
        case.cpt_code = submission.cpt_code
        case.payer_id = submission.payer_id
        case.inbound_bundle = bundle


async def _run_pipeline_async(case_id: str, bundle: dict) -> None:
    """Background task: runs intake + selector + adjudicator + reviewer + bundle
    builder, writing processing_stage between steps. Persists the final result."""

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
        await _update_case_stage(case_id, "complete")
        log.info(
            "doctor.pipeline_complete",
            case_id=case_id,
            outcome=run.determination.outcome if run.determination else None,
        )
    except Exception as e:
        log.exception("doctor.pipeline_failed", case_id=case_id, error=str(e))
        await _update_case_stage(case_id, "failed", error=str(e)[:1500])
