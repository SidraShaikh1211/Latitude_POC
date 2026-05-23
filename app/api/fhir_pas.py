"""Da Vinci PAS endpoints — the A2A simulation entry point."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException
from sqlalchemy import select

from app.api.cases import _persist
from app.db.engine import session_scope
from app.models import Case
from app.orchestrator import evaluate_pa_case
from app.pas.bundle_parser import BundleParseError


router = APIRouter(tags=["fhir-pas"])


@router.post("/fhir/Claim/$submit")
async def claim_submit(bundle: dict = Body(...)) -> dict[str, Any]:
    """Da Vinci PAS submission endpoint.

    Accepts an inbound Bundle with a Claim (use=preauthorization), runs the
    full pipeline, persists the case, and returns the PAS ClaimResponse
    Bundle as the response body.
    """
    try:
        run = await evaluate_pa_case(bundle, run_intake_on_documents=True)
    except BundleParseError as e:
        raise HTTPException(status_code=400, detail=f"Bundle parse error: {e}") from e
    await _persist(run)
    if run.response is None:
        raise HTTPException(status_code=500, detail="No ClaimResponse produced")
    return run.response.bundle


@router.get("/fhir/ClaimResponse/{case_id}")
async def get_claim_response(case_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if not case or not case.pas_response_bundle:
            raise HTTPException(status_code=404, detail="ClaimResponse not found")
        return case.pas_response_bundle
