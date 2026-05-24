"""Doctor-facing submission endpoint — PDF-only flow.

This module models the *doctor side* of the workflow as a separate entity
from the *payer side*. A doctor's submission is tracked as a `Submission`
row with a two-leg state machine:

    extracting_metadata → bundle_ready → sending → sent
        → awaiting_payer_response → payer_responded
                                  ↘ payer_failed
                                  ↘ failed (doctor-side)

The handoff is symmetric A2A: leg 1 is the doctor's POST to the payer's
`POST /fhir/Claim/$submit`; leg 2 is the payer POSTing the
`ClaimResponse` Bundle back to `POST /v1/doctor/inbound/claim-response`
(this module). The doctor's UI reads its own Submission row only — there
is no cross-coupling to the payer's Case row at the UI layer.

Endpoints:
  POST /v1/doctor/submit
      Accept a PDF, return a submission_id immediately. The submission
      moves through the doctor-side states on its own, then hands off to
      the payer. After handoff, the row sits in `awaiting_payer_response`
      until the payer's callback fires.
  GET  /v1/doctor/submissions
  GET  /v1/doctor/submissions/{submission_id}
      Read the doctor-side submission state.
  GET  /v1/doctor/submissions/{submission_id}/events
      SSE stream — closes on payer_responded | payer_failed | failed.
  POST /v1/doctor/inbound/claim-response
      Payer-callback receiver. Accepts a Da Vinci PAS ClaimResponse Bundle,
      finds the matching Submission row (case_id == submission_id), writes
      the outcome / narrative / missing-info / raw Bundle, and flips state.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
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


# Terminal Submission states — used by the SSE endpoint to decide when to
# close the stream. `sent` is no longer terminal: it just marks "doctor's
# push leg is done"; the row then waits in `awaiting_payer_response`.
TERMINAL_SUBMISSION_STATES = {"payer_responded", "payer_failed", "failed"}


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
        if snapshot["state"] in TERMINAL_SUBMISSION_STATES:
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

        # 2. Metadata extraction (Claude) — time + full Usage capture so the
        # Performance page can show the doctor-side leg of each run.
        meta_start = time.perf_counter()
        meta_result: MetadataResult = await extract_submission_metadata(
            doc, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename,
        )
        meta_duration = time.perf_counter() - meta_start
        meta_usage_fields = {
            "metadata_cost_usd": meta_result.usage.cost_usd,
            "metadata_input_tokens": meta_result.usage.input_tokens,
            "metadata_output_tokens": meta_result.usage.output_tokens,
            "metadata_cache_read_tokens": meta_result.usage.cache_read_tokens,
            "metadata_cache_creation_tokens": meta_result.usage.cache_creation_tokens,
            "metadata_duration_seconds": round(meta_duration, 3),
        }
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
                **meta_usage_fields,
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
            inbound_bundle=bundle,
            bundle_entry_count=len(bundle.get("entry", [])),
            bundle_size_bytes=len(json.dumps(bundle)),
            patient_display=patient_display,
            cpt_code=sub.cpt_code,
            **meta_usage_fields,
        )

        # Brief pause so the UI can render the "bundle ready" frame.
        await asyncio.sleep(0.5)

        # 4. Handoff: HTTP POST to the payer's PAS endpoint.
        await _update_submission(submission_id, state="sending")
        payer_case_id = await _post_to_payer(bundle)

        # 5. Push leg done — record the payer case id, then flip into the
        # waiting state. The row stays in `awaiting_payer_response` until
        # the payer POSTs the ClaimResponse Bundle back to
        # /v1/doctor/inbound/claim-response.
        await _update_submission(
            submission_id, state="sent", payer_case_id=payer_case_id,
        )
        await _update_submission(
            submission_id, state="awaiting_payer_response",
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
            "terminal": new_state in TERMINAL_SUBMISSION_STATES,
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
        "metadata_input_tokens": s.metadata_input_tokens,
        "metadata_output_tokens": s.metadata_output_tokens,
        "metadata_cache_read_tokens": s.metadata_cache_read_tokens,
        "metadata_cache_creation_tokens": s.metadata_cache_creation_tokens,
        "metadata_duration_seconds": s.metadata_duration_seconds,
        "bundle_entry_count": s.bundle_entry_count,
        "bundle_size_bytes": s.bundle_size_bytes,
        "bundle_preview": s.inbound_bundle,
        "patient_display": s.patient_display,
        "cpt_code": s.cpt_code,
        "payer_case_id": s.payer_case_id,
        "outcome": s.outcome,
        "determination_narrative": s.determination_narrative,
        "missing_info": s.missing_info,
        "claim_response_bundle": s.claim_response_bundle,
        "payer_responded_at": (
            s.payer_responded_at.isoformat() if s.payer_responded_at else None
        ),
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Inbound payer callback (leg 2 of the A2A round trip)
# ---------------------------------------------------------------------------


@router.post("/inbound/claim-response")
async def inbound_claim_response(bundle: dict = Body(...)) -> dict[str, Any]:
    """Receive a Da Vinci PAS `ClaimResponse` Bundle from the payer.

    This is the symmetric counterpart to the doctor's outbound
    `POST /fhir/Claim/$submit` — the payer POSTs the finalised
    ClaimResponse Bundle here once adjudication completes. We parse the
    outcome, reviewer narrative, and missing-info requests out of the
    Bundle, find the originating Submission row by `payer_case_id` (the
    payer generates its own case_id, independent of the doctor's
    submission_id — the FK on Submission is what links the two halves),
    and persist.
    """
    parsed = _parse_claim_response_bundle(bundle)
    payer_case_id = parsed["case_id"]
    if not payer_case_id:
        raise HTTPException(400, "ClaimResponse Bundle has no resolvable case id")

    async with session_scope() as session:
        result = await session.execute(
            select(Submission).where(Submission.payer_case_id == payer_case_id),
        )
        sub = result.scalar_one_or_none()
    if sub is None:
        raise HTTPException(
            404,
            f"No submission found with payer_case_id={payer_case_id!r}",
        )

    submission_id = sub.id
    await _update_submission(
        submission_id,
        state="payer_responded",
        outcome=parsed["outcome"],
        determination_narrative=parsed["narrative"],
        missing_info=parsed["missing_info"],
        claim_response_bundle=bundle,
        payer_responded_at=datetime.now(timezone.utc),
    )
    log.info(
        "doctor.claim_response_received",
        submission_id=submission_id,
        payer_case_id=payer_case_id,
        outcome=parsed["outcome"],
        missing_info_count=len(parsed["missing_info"]),
    )
    return {"submission_id": submission_id, "state": "payer_responded"}


def _parse_claim_response_bundle(bundle: dict) -> dict[str, Any]:
    """Extract the doctor-facing fields from a PAS ClaimResponse Bundle.

    Mirrors the layout produced by `app.pas.bundle_builder` — but doesn't
    couple to it: every field is read defensively so a slightly differently
    shaped Bundle from another payer would still work.
    """
    claim_response = _first_resource(bundle, "ClaimResponse") or {}

    # Payer-side case id: derive from Bundle.id `pas-response-<case_id>`,
    # then from ClaimResponse.id `claim-response-<case_id>`, then from
    # request.reference. The doctor side links it back via the
    # Submission.payer_case_id FK that was stored when /fhir/Claim/$submit
    # returned synchronously.
    case_id: str | None = None
    for candidate in (
        _strip_prefix(bundle.get("id"), "pas-response-"),
        _strip_prefix(claim_response.get("id"), "claim-response-"),
    ):
        if candidate:
            case_id = candidate
            break
    if not case_id:
        req_ref = (claim_response.get("request") or {}).get("reference") or ""
        if req_ref.startswith("Claim/"):
            case_id = req_ref.split("/", 1)[1]

    # Outcome: the bundle_builder writes the original 4-valued outcome
    # (approve/deny/pend/needs_human_review) into adjudication[0].category.text.
    # Fall back to the PAS valueset on ClaimResponse.outcome if absent.
    outcome: str | None = None
    items = claim_response.get("item") or []
    if items:
        adjs = (items[0] or {}).get("adjudication") or []
        if adjs:
            cat = (adjs[0] or {}).get("category") or {}
            text = cat.get("text")
            if isinstance(text, str) and text:
                outcome = text
    if outcome is None:
        outcome = _pas_outcome_to_determination(claim_response.get("outcome"))

    narrative = claim_response.get("disposition") or None

    missing_info: list[dict[str, str]] = []
    for note in claim_response.get("processNote") or []:
        text = note.get("text") or ""
        parsed = _parse_missing_info_note(text)
        if parsed:
            missing_info.append(parsed)

    return {
        "case_id": case_id,
        "outcome": outcome,
        "narrative": narrative,
        "missing_info": missing_info,
    }


def _first_resource(bundle: dict, resource_type: str) -> dict | None:
    for entry in bundle.get("entry") or []:
        res = entry.get("resource") or {}
        if res.get("resourceType") == resource_type:
            return res
    return None


def _strip_prefix(value: Any, prefix: str) -> str | None:
    if isinstance(value, str) and value.startswith(prefix):
        return value[len(prefix):] or None
    return None


# Reverse of bundle_builder._OUTCOME_MAP. Used only as a fallback — the
# adjudication category text is the authoritative source.
_PAS_TO_DETERMINATION = {
    "complete": "approve",
    "error": "deny",
    "queued": "pend",
    "partial": "pend",
}


def _pas_outcome_to_determination(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _PAS_TO_DETERMINATION.get(value)


def _parse_missing_info_note(text: str) -> dict[str, str] | None:
    """Parse a `[MISSING_INFO <id>] criterion=<cid> | request: <text>` note."""
    if not text.startswith("[MISSING_INFO "):
        return None
    try:
        header, _, rest = text.partition("] ")
        mi_id = header[len("[MISSING_INFO "):].strip()
        criterion_part, _, request_part = rest.partition("| request:")
        criterion_id = criterion_part.replace("criterion=", "").strip()
        request_text = request_part.strip()
        if not (mi_id and request_text):
            return None
        return {"id": mi_id, "criterion_id": criterion_id, "request": request_text}
    except Exception:
        return None
