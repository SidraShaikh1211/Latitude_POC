"""Performance metrics endpoint.

Aggregates the per-stage telemetry the orchestrator writes onto
`Case.metrics` together with the doctor-side metadata-extraction metrics
on `Submission` (joined by `Submission.payer_case_id`). Used by the
Performance page in the UI — one row per end-to-end run, with totals plus
the per-stage breakdown.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from app.db.engine import session_scope
from app.models import Case, Submission


router = APIRouter(prefix="/v1/metrics", tags=["metrics"])


@router.get("/runs")
async def list_runs(limit: int = 100) -> list[dict[str, Any]]:
    """Return recent runs joined doctor↔payer with metrics rolled up.

    A "run" is anchored on the Submission row; the Case (if any) is looked
    up via `Submission.payer_case_id`. A submission that never reached the
    payer still appears (with only doctor-side metrics) so failures stay
    visible.
    """
    async with session_scope() as session:
        sub_rows = (
            await session.execute(
                select(Submission)
                .order_by(Submission.created_at.desc())
                .limit(limit),
            )
        ).scalars().all()

        case_ids = [s.payer_case_id for s in sub_rows if s.payer_case_id]
        case_by_id: dict[str, Case] = {}
        if case_ids:
            case_rows = (
                await session.execute(select(Case).where(Case.id.in_(case_ids)))
            ).scalars().all()
            case_by_id = {c.id: c for c in case_rows}

    out: list[dict[str, Any]] = []
    for s in sub_rows:
        case = case_by_id.get(s.payer_case_id) if s.payer_case_id else None
        out.append(_build_run(s, case))
    return out


def _build_run(s: Submission, c: Case | None) -> dict[str, Any]:
    """Compose a single run row from the doctor Submission + payer Case."""
    doctor_stage = _doctor_metadata_stage(s)
    payer_metrics = (c.metrics if c else None) or {}
    payer_stages = list(payer_metrics.get("stages") or [])
    payer_leaves = list(payer_metrics.get("adjudication_leaves") or [])

    all_stages = ([doctor_stage] if doctor_stage else []) + payer_stages

    totals = _zero_totals()
    for st in all_stages:
        totals["tokens_in"] += st.get("tokens_in") or 0
        totals["tokens_out"] += st.get("tokens_out") or 0
        totals["cache_read"] += st.get("cache_read") or 0
        totals["cache_creation"] += st.get("cache_creation") or 0
        totals["cost_usd"] += st.get("cost_usd") or 0.0
        totals["llm_calls"] += st.get("llm_calls") or 0
    cacheable = totals["cache_read"] + totals["cache_creation"]
    totals["cache_hit_rate"] = (
        round(totals["cache_read"] / cacheable, 4) if cacheable else 0.0
    )
    totals["cost_usd"] = round(totals["cost_usd"], 6)

    doctor_duration = float(s.metadata_duration_seconds or 0.0)
    payer_duration = float(payer_metrics.get("duration_seconds") or 0.0)
    total_duration = round(doctor_duration + payer_duration, 3)

    return {
        "submission_id": s.id,
        "payer_case_id": s.payer_case_id,
        "submission_state": s.state,
        "case_status": c.status if c else None,
        "outcome": (c.outcome if c else None) or s.outcome,
        "patient_display": s.patient_display or (c.patient_display if c else None),
        "cpt_code": s.cpt_code or (c.cpt_code if c else None),
        "selected_policy_id": c.selected_policy_id if c else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "totals": totals,
        "duration_seconds": total_duration,
        "doctor_duration_seconds": round(doctor_duration, 3),
        "payer_duration_seconds": round(payer_duration, 3),
        "stages": all_stages,
        "adjudication_leaves": payer_leaves,
    }


def _doctor_metadata_stage(s: Submission) -> dict[str, Any] | None:
    """Format the doctor-side metadata-extraction step as one stage entry,
    matching the shape of the payer-side stages so the UI can render them
    uniformly. Returns None if no LLM call has happened yet."""
    if s.metadata_input_tokens is None and s.metadata_cost_usd is None:
        return None
    return {
        "name": "metadata_extraction",
        "side": "doctor",
        "duration_seconds": float(s.metadata_duration_seconds or 0.0),
        "tokens_in": int(s.metadata_input_tokens or 0),
        "tokens_out": int(s.metadata_output_tokens or 0),
        "cache_read": int(s.metadata_cache_read_tokens or 0),
        "cache_creation": int(s.metadata_cache_creation_tokens or 0),
        "cost_usd": float(s.metadata_cost_usd or 0.0),
        "llm_calls": 1,
    }


def _zero_totals() -> dict[str, Any]:
    return {
        "tokens_in": 0,
        "tokens_out": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "cost_usd": 0.0,
        "llm_calls": 0,
        "cache_hit_rate": 0.0,
    }
