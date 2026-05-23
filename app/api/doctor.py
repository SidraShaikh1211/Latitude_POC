"""Doctor-facing submission endpoint — PDF-only flow.

This module models the *doctor side* of the workflow as a separate entity
from the *payer side*. A doctor's submission is tracked as a `Submission`
row (state machine: extracting_metadata → bundle_ready → sending → sent
| failed). Only once the doctor has assembled a PAS Bundle and POSTed it
to the payer's `/fhir/Claim/$submit` endpoint does a `Case` come into
existence on the payer side.

POST /v1/doctor/submit
    Accepts a PDF, returns a submission_id immediately. The submission
    moves through the doctor-side states on its own, then hands off to the
    payer via a real HTTP POST to /fhir/Claim/$submit. The doctor's UI
    polls this endpoint until `state == "sent"` and `payer_case_id` is
    set, then switches to polling the payer case.

GET  /v1/doctor/submissions
GET  /v1/doctor/submissions/{submission_id}
    Read the doctor-side submission state.

The handoff to the payer uses HTTP (not a function call), so the
doctor → payer flow really is application-to-application even when both
halves are colocated on localhost.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import uuid
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, File, HTTPException, UploadFile
from sqlalchemy import select

from app.db.engine import session_scope
from app.extraction.metadata import (
    MetadataResult,
    extract_submission_metadata,
    missing_required_fields,
)
from app.extraction.pdf import extract_pdf
from app.models import Submission
from app.pas.bundle_constructor import SubmissionData, build_pas_bundle
from app.settings import settings


log = structlog.get_logger()

router = APIRouter(prefix="/v1/doctor", tags=["doctor"])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/submit")
async def doctor_submit(pdf: UploadFile = File(...)) -> dict[str, Any]:
    """Accept a PDF, kick off the doctor-side pipeline, return immediately.

    The actual work (metadata extraction, bundle assembly, handoff to payer)
    runs on a background task so the HTTP response stays snappy. The doctor
    UI polls GET /v1/doctor/submissions/{id} for live state.
    """
    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(400, "pdf upload is empty")
    pdf_filename = pdf.filename or "upload.pdf"

    submission_id = f"sub-{uuid.uuid4().hex[:10]}"

    async with session_scope() as session:
        sub = Submission(
            id=submission_id,
            state="extracting_metadata",
            pdf_filename=pdf_filename,
        )
        session.add(sub)

    asyncio.create_task(
        _run_doctor_pipeline_async(submission_id, pdf_bytes, pdf_filename),
    )

    return {
        "submission_id": submission_id,
        "state": "extracting_metadata",
        "pdf_filename": pdf_filename,
    }


@router.get("/submissions")
async def list_submissions(limit: int = 50) -> list[dict[str, Any]]:
    """List recent submissions (doctor's view)."""
    async with session_scope() as session:
        rows = await session.execute(
            select(Submission).order_by(Submission.updated_at.desc()).limit(limit),
        )
        return [_submission_to_dict(s) for s in rows.scalars().all()]


@router.get("/submissions/{submission_id}")
async def get_submission(submission_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        sub = await session.get(Submission, submission_id)
        if not sub:
            raise HTTPException(404, "Submission not found")
        return _submission_to_dict(sub)


# ---------------------------------------------------------------------------
# Background pipeline
# ---------------------------------------------------------------------------


async def _run_doctor_pipeline_async(
    submission_id: str, pdf_bytes: bytes, pdf_filename: str,
) -> None:
    """The doctor side: PDF → metadata → bundle → POST to payer.

    Writes Submission state at every boundary so the UI's polling reflects
    progress live.
    """
    try:
        # 1. Read PDF (deterministic, no LLM)
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(pdf_bytes)
            tmp_path = f.name
        doc = extract_pdf(tmp_path, document_id=f"upload-{submission_id}")
        log.info("doctor.metadata_extracting", submission_id=submission_id, pages=doc.page_count)

        # 2. Metadata extraction (Claude)
        meta_result: MetadataResult = await extract_submission_metadata(
            doc, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename,
        )
        missing = missing_required_fields(meta_result.metadata)
        if missing:
            await _update_submission(
                submission_id,
                state="failed",
                error=(
                    f"PDF did not contain enough information to assemble a PAS "
                    f"submission. Missing: {', '.join(missing)}."
                ),
                extracted_metadata=meta_result.metadata.model_dump(),
                extraction_notes=meta_result.notes,
                metadata_cost_usd=meta_result.usage.cost_usd,
            )
            return

        # 3. Bundle assembly (deterministic, doctor-side only)
        sub: SubmissionData = meta_result.submission
        bundle, _ = build_pas_bundle(sub, case_id=submission_id)
        patient_display = f"{sub.patient_given} {sub.patient_family}".strip()

        await _update_submission(
            submission_id,
            state="bundle_ready",
            extracted_metadata=meta_result.metadata.model_dump(),
            extraction_notes=meta_result.notes,
            metadata_cost_usd=meta_result.usage.cost_usd,
            inbound_bundle=bundle,
            bundle_entry_count=len(bundle.get("entry", [])),
            bundle_size_bytes=len(json.dumps(bundle)),
            patient_display=patient_display,
            cpt_code=sub.cpt_code,
        )

        # Brief pause so the UI can render the "bundle ready" frame.
        await asyncio.sleep(0.5)

        # 4. Handoff: HTTP POST to the payer's PAS endpoint.
        await _update_submission(submission_id, state="sending")
        payer_case_id = await _post_to_payer(bundle)

        # 5. Done — record the payer case id for cross-navigation.
        await _update_submission(
            submission_id, state="sent", payer_case_id=payer_case_id,
        )
        log.info(
            "doctor.handoff_complete",
            submission_id=submission_id,
            payer_case_id=payer_case_id,
        )

    except Exception as e:
        log.exception("doctor.pipeline_failed", submission_id=submission_id)
        await _update_submission(submission_id, state="failed", error=str(e)[:1500])


async def _post_to_payer(bundle: dict) -> str:
    """Hand the assembled Bundle to the payer via HTTP POST.

    This is an explicit network call (loopback in dev) so the doctor → payer
    boundary is a real A2A handoff, not an in-process function call.
    """
    base = settings.payer_pas_base_url
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{base}/fhir/Claim/$submit", json=bundle)
        resp.raise_for_status()
        data = resp.json()
    case_id = data.get("case_id")
    if not case_id:
        raise RuntimeError(f"Payer did not return a case_id: {data}")
    return case_id


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _update_submission(submission_id: str, **fields: Any) -> None:
    """Patch a Submission row. `error` maps to `error_message`."""
    async with session_scope() as session:
        sub = await session.get(Submission, submission_id)
        if sub is None:
            log.warning("doctor.submission_missing", submission_id=submission_id)
            return
        if "error" in fields:
            sub.error_message = (fields.pop("error") or "")[:2048] or None
        for k, v in fields.items():
            setattr(sub, k, v)


def _submission_to_dict(s: Submission) -> dict[str, Any]:
    return {
        "submission_id": s.id,
        "state": s.state,
        "pdf_filename": s.pdf_filename,
        "error_message": s.error_message,
        "extracted_metadata": s.extracted_metadata,
        "extraction_notes": s.extraction_notes,
        "metadata_cost_usd": s.metadata_cost_usd,
        "bundle_entry_count": s.bundle_entry_count,
        "bundle_size_bytes": s.bundle_size_bytes,
        "bundle_preview": s.inbound_bundle,
        "patient_display": s.patient_display,
        "cpt_code": s.cpt_code,
        "payer_case_id": s.payer_case_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
