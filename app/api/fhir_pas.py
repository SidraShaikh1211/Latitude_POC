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

import httpx
import structlog
from fastapi import APIRouter, Body, HTTPException, Query

from app import events
from app.api.cases import _persist, _update_case_stage
from app.db.engine import session_scope
from app.models import Case
from app.orchestrator import evaluate_pa_case
from app.pas.bundle_parser import BundleParseError
from app.settings import settings


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


# Wall-clock ceiling on the payer-side pipeline. With per-request Anthropic
# timeouts of 60s, max_retries=2, and an 8-iteration agent loop per leaf
# (each leaf wrapped in a 120s `asyncio.wait_for` budget in adjudicate_all),
# the real-world cost-tuned target is ~3-4 min. 10 min is the hard ceiling
# — a pipeline taking longer is wedged and should fail loudly instead of
# holding the row in `processing` until the next server restart.
PIPELINE_DEADLINE_SECONDS = 600.0


async def _run_pipeline_async(case_id: str, bundle: dict) -> None:
    """Background task: run the orchestrator, persisting partial results
    after each step so a polling UI sees data appear progressively, then
    write the final case snapshot."""
    async def stage_cb(stage: str) -> None:
        await _update_case_stage(case_id, stage)

    async def partial_cb(fields: dict[str, Any]) -> None:
        await _apply_partial(case_id, fields)

    try:
        run = await asyncio.wait_for(
            evaluate_pa_case(
                bundle,
                run_intake_on_documents=True,
                case_id=case_id,
                progress_callback=stage_cb,
                on_partial=partial_cb,
            ),
            timeout=PIPELINE_DEADLINE_SECONDS,
        )
        # Final snapshot: idempotently writes the same fields plus the
        # terminal status (approved/denied/pended/needs_review).
        await _persist(run)
        await _update_case_stage(case_id, "complete")
        log.info(
            "pas.pipeline_complete",
            case_id=case_id,
            outcome=run.determination.outcome if run.determination else None,
        )
        # Symmetric A2A: POST the ClaimResponse Bundle back to the doctor
        # side so the doctor's UI gets the verdict over the wire, not by
        # reading the payer's Case row. Fire-and-forget — failures are
        # logged but don't fail the pipeline (the Case row remains the
        # source of truth payer-side).
        if run.response is not None:
            asyncio.create_task(
                _post_claim_response_to_doctor(case_id, run.response.bundle),
            )
    except asyncio.TimeoutError:
        log.error("pas.pipeline_timeout", case_id=case_id, deadline=PIPELINE_DEADLINE_SECONDS)
        await _update_case_stage(
            case_id,
            "failed",
            error=(
                f"Pipeline exceeded the {PIPELINE_DEADLINE_SECONDS:.0f}s deadline. "
                "The LLM call chain stalled. Resubmit the case to retry."
            ),
        )
    except Exception as e:
        log.exception("pas.pipeline_failed", case_id=case_id, error=str(e))
        await _update_case_stage(case_id, "failed", error=str(e)[:1500])


async def _apply_partial(case_id: str, fields: dict[str, Any]) -> None:
    """Patch a Case row with a partial set of column values.

    Fields are the kwargs of `Case(...)` minus `id`. Unknown keys are
    silently dropped (defensive — caller is trusted but mismatches are
    cheap to tolerate).
    """
    if not fields:
        return
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if case is None:
            log.warning("pas.partial_missing_case", case_id=case_id)
            return
        for k, v in fields.items():
            if hasattr(case, k):
                setattr(case, k, v)

    # Forward to SSE subscribers. We only forward field NAMES + scalar values
    # here; large blobs like extracted_facts can be fetched via GET when the
    # client wants the full payload.
    await events.publish(
        f"case:{case_id}",
        {"type": "partial", "fields": list(fields.keys())},
    )


async def _post_claim_response_to_doctor(case_id: str, response_bundle: dict) -> None:
    """Symmetric A2A callback: POST the ClaimResponse Bundle back to the
    doctor side so the doctor's UI gets the verdict via HTTP, not by
    reading the payer's Case row.

    Best-effort: a callback failure does not roll the Case status back.
    Production would queue a retry; for the prototype we just log loudly.
    """
    url = f"{settings.doctor_callback_base_url}/v1/doctor/inbound/claim-response"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=response_bundle)
            resp.raise_for_status()
        log.info(
            "pas.doctor_callback_sent",
            case_id=case_id,
            url=url,
            status_code=resp.status_code,
        )
    except Exception as e:
        log.error(
            "pas.doctor_callback_failed",
            case_id=case_id,
            url=url,
            error=str(e)[:500],
        )


@router.get("/fhir/ClaimResponse/{case_id}")
async def get_claim_response(case_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if not case or not case.pas_response_bundle:
            raise HTTPException(status_code=404, detail="ClaimResponse not found")
        return case.pas_response_bundle
