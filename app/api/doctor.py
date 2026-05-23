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
import os
import tempfile
import uuid
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app import events
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


@router.get("/submissions/{submission_id}/events")
async def submission_events(submission_id: str, request: Request) -> StreamingResponse:
    """SSE stream of submission lifecycle events.

    First event is a `snapshot` with the current submission state. Then each
    subsequent state change (extracting_metadata → bundle_ready → sending
    → sent/failed) emits an `update` event carrying the fresh snapshot. The
    stream closes after `sent` or `failed`.
    """

    async def event_source():
        async with session_scope() as session:
            sub = await session.get(Submission, submission_id)
        if sub is None:
            yield _sse_event("error", {"detail": "Submission not found"})
            return

        snapshot = _submission_to_dict(sub)
        yield _sse_event("snapshot", snapshot)
        if snapshot["state"] in ("sent", "failed"):
            return

        queue = events.subscribe(f"submission:{submission_id}")
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                async with session_scope() as session:
                    sub = await session.get(Submission, submission_id)
                fresh = _submission_to_dict(sub) if sub else None
                yield _sse_event("update", {
                    "fields": ev.get("fields", []),
                    "snapshot": fresh,
                })
                if ev.get("terminal"):
                    return
        finally:
            events.unsubscribe(f"submission:{submission_id}", queue)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_source(), media_type="text/event-stream", headers=headers)


def _sse_event(event_name: str, data: Any) -> str:
    payload = json.dumps(data, default=str)
    return f"event: {event_name}\ndata: {payload}\n\n"


# ---------------------------------------------------------------------------
# Background pipeline
# ---------------------------------------------------------------------------


async def _run_doctor_pipeline_async(
    submission_id: str, pdf_bytes: bytes, pdf_filename: str,
) -> None:
    """The doctor side: PDF → metadata → bundle → POST to payer.

    Writes Submission state at every boundary so the UI's polling reflects
    progress live. Wrapped in `asyncio.wait_for` so a stalled Claude call
    can't hold the Submission row in a non-terminal state indefinitely.
    """
    try:
        await asyncio.wait_for(
            _run_doctor_pipeline_inner(submission_id, pdf_bytes, pdf_filename),
            timeout=DOCTOR_PIPELINE_DEADLINE_SECONDS,
        )
    except asyncio.TimeoutError:
        log.error(
            "doctor.pipeline_timeout",
            submission_id=submission_id,
            deadline=DOCTOR_PIPELINE_DEADLINE_SECONDS,
        )
        await _update_submission(
            submission_id,
            state="failed",
            error=(
                f"Submission pipeline exceeded the "
                f"{DOCTOR_PIPELINE_DEADLINE_SECONDS:.0f}s deadline (likely a stalled "
                "metadata extraction). Resubmit the PDF to retry."
            ),
        )
    except Exception as e:
        log.exception("doctor.pipeline_failed", submission_id=submission_id)
        await _update_submission(submission_id, state="failed", error=str(e)[:1500])


# Doctor-side ceiling: PDF read + one Claude structured-output + bundle
# assemble + one HTTP handoff. 3 min is generous; production would be ~30s.
DOCTOR_PIPELINE_DEADLINE_SECONDS = 180.0


async def _run_doctor_pipeline_inner(
    submission_id: str, pdf_bytes: bytes, pdf_filename: str,
) -> None:
    """Doctor pipeline body. Exceptions propagate to the wrapper, which
    converts them into a `failed` Submission row."""
    tmp_path: str | None = None
    try:
        # 1. Read PDF (deterministic, no LLM). Stage the bytes through a
        # temp file because pymupdf.open() wants a path.
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
    finally:
        # PyMuPDF has finished with the file by the time we exit the function
        # body (extract_pdf opens with `with pymupdf.open(...)`), so it's
        # safe to unlink even on the happy path.
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
        new_state = sub.state
        payer_case_id = sub.payer_case_id

    # Fan out to any SSE subscriber listening on this submission
    await events.publish(
        f"submission:{submission_id}",
        {
            "type": "update",
            "state": new_state,
            "payer_case_id": payer_case_id,
            "fields": list(fields.keys()),
            "terminal": new_state in ("sent", "failed"),
        },
    )


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
