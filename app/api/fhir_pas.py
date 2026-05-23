"""Da Vinci PAS endpoints — the A2A entry point.

`POST /fhir/Claim/$submit` is the contractual handoff from a submitting
agent (doctor's EHR, in this prototype: the doctor's flow internally POSTs
here). It accepts an inbound PAS Claim Bundle, immediately persists a Case
in `received` state, fires the orchestrator on a background task, and
returns `{case_id, status, processing_stage}` so the caller can poll for
progress.

For tests / scripts that want the synchronous behaviour (run the whole
pipeline inline and return the ClaimResponse Bundle), call
`POST /fhir/Claim/$submit?wait=true`.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Body, HTTPException, Query

from app.api.cases import _persist, _update_case_stage
from app.db.engine import session_scope
from app.models import Case
from app.orchestrator import evaluate_pa_case
from app.pas.bundle_parser import BundleParseError


log = structlog.get_logger()

router = APIRouter(tags=["fhir-pas"])


@router.post("/fhir/Claim/$submit")
async def claim_submit(
    bundle: dict = Body(...),
    wait: bool = Query(False, description="If true, run the pipeline synchronously and return the ClaimResponse Bundle."),
) -> dict[str, Any]:
    """Da Vinci PAS submission endpoint.

    Default behaviour (async, fire-and-poll):
        1. Persist a new Case row in `received` state.
        2. Fire the orchestrator on a background task.
        3. Return {case_id, status: "accepted", processing_stage: "received"}.
        The caller (typically the doctor's submission flow) polls
        `GET /v1/cases/{case_id}` for progress.

    Synchronous behaviour (`?wait=true`):
        1. Run the orchestrator inline.
        2. Persist the case.
        3. Return the ClaimResponse Bundle directly.
        Used by integration scripts and direct A2A callers that expect the
        PAS synchronous contract.
    """
    # Pre-create the Case row so the payer-side stage is visible from the
    # moment the Bundle arrives — but NOT before. This is the boundary that
    # separates "doctor still preparing the request" from "payer received it".
    case_id = f"case-{uuid.uuid4().hex[:10]}"

    if wait:
        try:
            run = await evaluate_pa_case(
                bundle, run_intake_on_documents=True, case_id=case_id
            )
        except BundleParseError as e:
            raise HTTPException(status_code=400, detail=f"Bundle parse error: {e}") from e
        await _persist(run)
        if run.response is None:
            raise HTTPException(status_code=500, detail="No ClaimResponse produced")
        return run.response.bundle

    # Async path — quick parse-check, persist a placeholder Case, fire the
    # pipeline in the background.
    try:
        from app.pas.bundle_parser import parse_pas_bundle
        parsed = parse_pas_bundle(bundle)
    except BundleParseError as e:
        raise HTTPException(status_code=400, detail=f"Bundle parse error: {e}") from e

    patient_display = None
    if parsed.facts.patient:
        name = (parsed.facts.patient.get("name") or [{}])[0]
        family = name.get("family", "")
        given = " ".join(name.get("given") or [])
        patient_display = f"{given} {family}".strip() or None

    async with session_scope() as session:
        case = Case(
            id=case_id,
            status="processing",
            processing_stage="received",
            patient_display=patient_display,
            cpt_code=parsed.context.cpt_code,
            payer_id=parsed.context.payer_id,
            inbound_bundle=bundle,
        )
        session.add(case)

    asyncio.create_task(_run_pipeline_async(case_id, bundle))

    log.info("pas.claim_submit_accepted", case_id=case_id, patient=patient_display)

    return {
        "case_id": case_id,
        "status": "accepted",
        "processing_stage": "received",
    }


async def _run_pipeline_async(case_id: str, bundle: dict) -> None:
    """Background task: run the orchestrator and persist the final case."""
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
            "pas.pipeline_complete",
            case_id=case_id,
            outcome=run.determination.outcome if run.determination else None,
        )
    except Exception as e:
        log.exception("pas.pipeline_failed", case_id=case_id, error=str(e))
        await _update_case_stage(case_id, "failed", error=str(e)[:1500])


@router.get("/fhir/ClaimResponse/{case_id}")
async def get_claim_response(case_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if not case or not case.pas_response_bundle:
            raise HTTPException(status_code=404, detail="ClaimResponse not found")
        return case.pas_response_bundle
