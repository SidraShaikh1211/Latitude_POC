"""Case-management REST endpoints."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select

from app import events
from app.db.engine import session_scope
from app.models import Case
from app.orchestrator import CaseRun, evaluate_pa_case


router = APIRouter(prefix="/v1", tags=["cases"])


class EvaluateRequest(BaseModel):
    bundle: dict
    case_id: str | None = None
    run_intake: bool = True


class CaseSummary(BaseModel):
    case_id: str
    status: str
    outcome: str | None
    selected_policy_id: str | None
    branch: str | None
    patient_display: str | None
    cpt_code: str | None
    payer_id: str | None


def _to_db_status(outcome: str | None) -> str:
    return {
        "approve": "approved",
        "deny": "denied",
        "pend": "pended",
        "needs_human_review": "needs_review",
    }.get(outcome or "", "unknown")


async def _update_case_stage(
    case_id: str,
    stage: str,
    *,
    error: str | None = None,
) -> None:
    """Incrementally write the processing_stage (and optionally error_message)
    of an existing Case. Used by the doctor-submit background task to update
    progress between pipeline steps.

    Quietly no-ops if the case row doesn't exist yet — the caller can pre-create
    the row before kicking off the background pipeline.
    """
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if case is None:
            return
        case.processing_stage = stage
        if error is not None:
            case.error_message = error[:2048]
        if stage == "complete":
            # Status will be set definitively by _persist(run) at the end;
            # we only touch stage here.
            pass
        elif stage == "failed":
            case.status = "failed"

    # Fan out to any SSE subscriber listening on this case
    await events.publish(
        f"case:{case_id}",
        {
            "type": "stage",
            "stage": stage,
            "error": error,
            "terminal": stage in ("complete", "failed"),
        },
    )


async def _persist(run: CaseRun) -> None:
    patient_display = None
    if run.parsed_in.facts.patient:
        name = (run.parsed_in.facts.patient.get("name") or [{}])[0]
        family = name.get("family", "")
        given = " ".join(name.get("given") or [])
        patient_display = f"{given} {family}".strip()

    async with session_scope() as session:
        case = Case(
            id=run.case_id,
            status=_to_db_status(run.determination.outcome if run.determination else None),
            patient_display=patient_display,
            cpt_code=run.parsed_in.context.cpt_code,
            payer_id=run.parsed_in.context.payer_id,
            selected_policy_id=run.selection.selected_policy_id,
            branch=run.selection.branch,
            outcome=run.determination.outcome if run.determination else None,
            inbound_bundle=run.parsed_in.raw_bundle,
            extracted_facts=(
                run.intake.facts.model_dump() if run.intake else None
            ),
            policy_selection={
                "status": run.selection.status,
                "selected_policy_id": run.selection.selected_policy_id,
                "branch": run.selection.branch,
                "selection_reason": run.selection.selection_reason,
                "eliminated": run.selection.eliminated,
            },
            criteria_evaluation=(
                {
                    "leaf_verdicts": {
                        cid: v.model_dump() for cid, v in run.adjudication.leaf_verdicts.items()
                    },
                    "exclusion_verdicts": {
                        eid: v.model_dump() for eid, v in run.adjudication.exclusion_verdicts.items()
                    },
                }
                if run.adjudication else None
            ),
            determination=(
                {
                    "outcome": run.determination.outcome,
                    "rationale": run.determination.rationale,
                    "triggered_exclusions": run.determination.triggered_exclusions,
                    "escalation_reasons": run.determination.escalation_reasons,
                    "narrative": run.reviewer.output.narrative if run.reviewer else None,
                    "missing_info": [
                        mi.model_dump() for mi in (run.reviewer.output.missing_info if run.reviewer else [])
                    ],
                }
                if run.determination else None
            ),
            pas_response_bundle=run.response.bundle if run.response else None,
            metrics=run.metrics,
            processing_stage="complete",
        )
        # Upsert: if exists, replace fields
        existing = await session.get(Case, run.case_id)
        if existing:
            for col in (
                "status", "patient_display", "cpt_code", "payer_id",
                "selected_policy_id", "branch", "outcome", "inbound_bundle",
                "extracted_facts", "policy_selection", "criteria_evaluation",
                "determination", "pas_response_bundle", "metrics",
                "processing_stage",
            ):
                setattr(existing, col, getattr(case, col))
        else:
            session.add(case)


@router.post("/cases/evaluate", response_model=dict)
async def evaluate_case(req: EvaluateRequest) -> dict[str, Any]:
    """Run the full pipeline on an inbound PAS Bundle and persist the case."""
    run = await evaluate_pa_case(
        req.bundle, run_intake_on_documents=req.run_intake, case_id=req.case_id
    )
    await _persist(run)
    return run.summary()


@router.get("/cases", response_model=list[CaseSummary])
async def list_cases() -> list[CaseSummary]:
    async with session_scope() as session:
        rows = await session.execute(select(Case).order_by(Case.updated_at.desc()).limit(100))
        return [
            CaseSummary(
                case_id=c.id, status=c.status, outcome=c.outcome,
                selected_policy_id=c.selected_policy_id, branch=c.branch,
                patient_display=c.patient_display, cpt_code=c.cpt_code,
                payer_id=c.payer_id,
            )
            for c in rows.scalars().all()
        ]


@router.get("/cases/{case_id}")
async def get_case(case_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if not case:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
        return {
            "case_id": case.id,
            "status": case.status,
            "processing_stage": case.processing_stage,
            "error_message": case.error_message,
            "outcome": case.outcome,
            "selected_policy_id": case.selected_policy_id,
            "branch": case.branch,
            "patient_display": case.patient_display,
            "cpt_code": case.cpt_code,
            "payer_id": case.payer_id,
            "extracted_facts": case.extracted_facts,
            "policy_selection": case.policy_selection,
            "criteria_evaluation": case.criteria_evaluation,
            "determination": case.determination,
            "pas_response_bundle": case.pas_response_bundle,
            "metrics": case.metrics,
            "created_at": case.created_at.isoformat() if case.created_at else None,
            "updated_at": case.updated_at.isoformat() if case.updated_at else None,
        }


@router.get("/cases/{case_id}/events")
async def case_events(case_id: str, request: Request) -> StreamingResponse:
    """SSE stream of case lifecycle events.

    The first event is always a `snapshot` carrying the current case state,
    so a late-subscribing client sees the case in whatever stage it's in
    (including already-complete) without having to also GET /cases/{id}.

    Subsequent events:
      - `stage`   — { stage: "<intake|selecting|adjudicating|reviewing|building_response|complete|failed>", terminal: bool }
      - `partial` — { fields: [<column names that were just written>] }

    The stream closes after a terminal `stage` event.
    """

    async def event_source():
        # Validate up-front so we don't open a stream against a missing case
        async with session_scope() as session:
            case = await session.get(Case, case_id)
        if case is None:
            yield _sse_event("error", {"detail": "Case not found"})
            return

        # Initial snapshot — same payload shape as GET /cases/{id}
        snapshot = await get_case(case_id)
        yield _sse_event("snapshot", snapshot)
        if snapshot["processing_stage"] in ("complete", "failed"):
            return

        # Live subscription. Stream until we get a terminal stage or the
        # client disconnects (request.is_disconnected() goes true).
        queue = events.subscribe(f"case:{case_id}")
        try:
            while True:
                if await request.is_disconnected():
                    return
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Keep-alive comment so proxies don't close the pipe
                    yield ": keepalive\n\n"
                    continue
                # Re-fetch the snapshot so every push carries the latest
                # resolved case rather than just an event envelope. Without
                # this, frontends that only know how to consume snapshots
                # would miss stage transitions like intake→adjudicating.
                if ev.get("type") in ("partial", "stage"):
                    fresh = await get_case(case_id)
                    yield _sse_event(ev["type"], {
                        **{k: v for k, v in ev.items() if k != "type"},
                        "snapshot": fresh,
                    })
                else:
                    yield _sse_event(ev.get("type", "message"), ev)
                if ev.get("terminal"):
                    # Send a final snapshot so the client has the determination
                    fresh = await get_case(case_id)
                    yield _sse_event("complete", fresh)
                    return
        finally:
            events.unsubscribe(f"case:{case_id}", queue)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # disable proxy buffering (nginx)
    }
    return StreamingResponse(event_source(), media_type="text/event-stream", headers=headers)


def _sse_event(event_name: str, data: Any) -> str:
    """Format a single SSE frame. `data` is JSON-serialized."""
    payload = json.dumps(data, default=str)
    return f"event: {event_name}\ndata: {payload}\n\n"


@router.get("/cases/{case_id}/determination")
async def get_determination(case_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        case = await session.get(Case, case_id)
        if not case:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
        if not case.determination:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not yet evaluated")
        return case.determination
