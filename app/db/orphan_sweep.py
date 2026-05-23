"""Startup orphan-sweep for in-flight Cases and Submissions.

When uvicorn restarts (intentionally or via `--reload` triggering on a file
edit), any background `asyncio.create_task` running the orchestrator or the
doctor pipeline is silently killed. The row left behind shows a non-terminal
`processing_stage` / `state` and never advances, which makes the UI poll
forever.

On startup, mark every row that's older than `STALE_AFTER` and still in a
non-terminal state as `failed`, with an error message that makes the cause
obvious. This is conservative: a freshly-started case (<5 min old) is left
alone in case it's genuinely in flight on a sibling worker.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update

from app.db.engine import session_scope
from app.models import Case, Submission


log = structlog.get_logger()


STALE_AFTER = timedelta(minutes=5)

# Stages that mean "the background task should still be running."
IN_FLIGHT_CASE_STAGES = (
    "received",
    "parsing",
    "intake",
    "selecting",
    "adjudicating",
    "reviewing",
    "building_response",
)

# Submission states that mean "the doctor-side background task should still be running."
IN_FLIGHT_SUBMISSION_STATES = (
    "extracting_metadata",
    "bundle_ready",
    "sending",
)


async def sweep_orphans() -> dict[str, int]:
    """Mark stale in-flight Cases and Submissions as failed. Returns counts."""
    cutoff = datetime.now(timezone.utc) - STALE_AFTER

    cases_marked = 0
    submissions_marked = 0

    async with session_scope() as session:
        # Cases — any row with status="processing" + an in-flight stage +
        # untouched for > STALE_AFTER is orphaned.
        case_q = select(Case).where(
            Case.status == "processing",
            Case.processing_stage.in_(IN_FLIGHT_CASE_STAGES),
            Case.updated_at < cutoff,
        )
        for c in (await session.execute(case_q)).scalars().all():
            stage_at_death = c.processing_stage
            c.status = "failed"
            c.processing_stage = "failed"
            c.error_message = (
                f"Pipeline orphaned by server restart while at stage='{stage_at_death}'. "
                f"Resubmit the case to retry. (Detected by orphan_sweep on startup.)"
            )
            cases_marked += 1
            log.warning(
                "orphan_sweep.case_failed",
                case_id=c.id,
                stage_at_death=stage_at_death,
                updated_at=c.updated_at.isoformat() if c.updated_at else None,
            )

        # Submissions — same idea on the doctor side. Don't touch ones that
        # already reached `sent` (those are handed off to a Case row that the
        # case-side sweep handles).
        sub_q = select(Submission).where(
            Submission.state.in_(IN_FLIGHT_SUBMISSION_STATES),
            Submission.updated_at < cutoff,
        )
        for s in (await session.execute(sub_q)).scalars().all():
            state_at_death = s.state
            s.state = "failed"
            s.error_message = (
                f"Doctor-side pipeline orphaned by server restart while in "
                f"state='{state_at_death}'. Resubmit the PDF to retry. "
                "(Detected by orphan_sweep on startup.)"
            )
            submissions_marked += 1
            log.warning(
                "orphan_sweep.submission_failed",
                submission_id=s.id,
                state_at_death=state_at_death,
                updated_at=s.updated_at.isoformat() if s.updated_at else None,
            )

    log.info(
        "orphan_sweep.done",
        cases_marked_failed=cases_marked,
        submissions_marked_failed=submissions_marked,
        cutoff=cutoff.isoformat(),
    )
    return {"cases": cases_marked, "submissions": submissions_marked}
