"""MCP server (stdio transport).

Exposes the same backend the REST API uses, so an MCP client (e.g., Claude
Desktop) can call `evaluate_prior_auth` on a PAS Bundle, browse loaded
policies, and inspect prior cases.

Tools:
  - evaluate_prior_auth(bundle)
  - submit_pas_claim(bundle)           [alias for evaluate_prior_auth]
  - get_case(case_id)
  - list_cases()
  - get_policy(policy_id)
  - list_policies()

Resources (URI-addressable):
  - case://{case_id}      → JSON of a stored case
  - policy://{policy_id}  → JSON of a policy with full criteria tree

Prompts:
  - pa_review_summary     → "Summarize this PA case in 3 sentences"
  - appeal_letter_draft   → "Draft an appeal letter for the denied case"

Run with:
    python -m app.mcp_server.server
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import structlog
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    GetPromptResult,
    Prompt,
    PromptArgument,
    PromptMessage,
    Resource,
    TextContent,
    Tool,
)
from sqlalchemy import select

from app.db.engine import init_db, session_scope
from app.logging_config import configure_logging
from app.models import Case
from app.orchestrator import evaluate_pa_case
from app.policy.registry import get_registry, iter_leaves


log = structlog.get_logger()

server = Server("latitude-health-pa")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="evaluate_prior_auth",
            description=(
                "Run the full prior-authorization pipeline on a Da Vinci PAS "
                "Bundle (Claim/Patient/Coverage/...). Returns determination + "
                "ClaimResponse Bundle. Accepts the inbound Bundle as a JSON object."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "bundle": {
                        "type": "object",
                        "description": "Da Vinci PAS Claim Bundle (FHIR R4)",
                    },
                    "case_id": {"type": "string"},
                    "run_intake": {"type": "boolean", "default": True},
                },
                "required": ["bundle"],
            },
        ),
        Tool(
            name="submit_pas_claim",
            description="Alias for evaluate_prior_auth. Use whichever name fits your workflow.",
            inputSchema={
                "type": "object",
                "properties": {"bundle": {"type": "object"}},
                "required": ["bundle"],
            },
        ),
        Tool(
            name="get_case",
            description="Retrieve a stored case by case_id, including verdicts + determination + outbound Bundle.",
            inputSchema={
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
            },
        ),
        Tool(
            name="list_cases",
            description="List recent cases (case_id, status, outcome, patient, CPT).",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_policy",
            description="Return a policy's full criteria tree + exclusions + applies_to.",
            inputSchema={
                "type": "object",
                "properties": {"policy_id": {"type": "string"}},
                "required": ["policy_id"],
            },
        ),
        Tool(
            name="list_policies",
            description="List loaded policies (policy_id, name, payer, version, leaf_count, exclusion_count).",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        if name in ("evaluate_prior_auth", "submit_pas_claim"):
            bundle = arguments["bundle"]
            case_id = arguments.get("case_id")
            run_intake = arguments.get("run_intake", True)
            run = await evaluate_pa_case(
                bundle, run_intake_on_documents=run_intake, case_id=case_id
            )
            # Persist via the same path as REST
            from app.api.cases import _persist
            await _persist(run)
            payload = {
                **run.summary(),
                "pas_response_bundle": run.response.bundle if run.response else None,
            }
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "get_case":
            case_id = arguments["case_id"]
            async with session_scope() as session:
                case = await session.get(Case, case_id)
                if not case:
                    return [TextContent(type="text", text=json.dumps({"error": "not found"}))]
                payload = {
                    "case_id": case.id, "status": case.status, "outcome": case.outcome,
                    "selected_policy_id": case.selected_policy_id, "branch": case.branch,
                    "policy_selection": case.policy_selection,
                    "criteria_evaluation": case.criteria_evaluation,
                    "determination": case.determination,
                    "pas_response_bundle": case.pas_response_bundle,
                }
                return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "list_cases":
            async with session_scope() as session:
                rows = await session.execute(select(Case).order_by(Case.updated_at.desc()).limit(50))
                summaries = [
                    {"case_id": c.id, "status": c.status, "outcome": c.outcome,
                     "patient": c.patient_display, "cpt_code": c.cpt_code}
                    for c in rows.scalars().all()
                ]
                return [TextContent(type="text", text=json.dumps(summaries, indent=2))]

        if name == "get_policy":
            from app.api.policies import get_policy as get_policy_payload
            payload = get_policy_payload(arguments["policy_id"])
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        if name == "list_policies":
            reg = get_registry()
            payload = []
            for p in reg.all_policies():
                payload.append({
                    "policy_id": p.policy_id, "name": p.name, "payer_id": p.payer_id,
                    "version": p.version, "leaf_count": sum(1 for _ in iter_leaves(p.criteria)),
                    "exclusion_count": len(p.exclusions),
                })
            return [TextContent(type="text", text=json.dumps(payload, indent=2))]

        return [TextContent(type="text", text=json.dumps({"error": f"unknown tool {name}"}))]
    except Exception as e:
        log.error("mcp.tool_failed", tool=name, error=str(e))
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------


@server.list_resources()
async def list_resources() -> list[Resource]:
    items: list[Resource] = []
    # Policies
    for p in get_registry().all_policies():
        items.append(Resource(
            uri=f"policy://{p.policy_id}",
            name=f"Policy: {p.name}",
            description=f"{p.policy_id} v{p.version} (payer={p.payer_id})",
            mimeType="application/json",
        ))
    # Cases
    async with session_scope() as session:
        rows = await session.execute(select(Case).order_by(Case.updated_at.desc()).limit(20))
        for c in rows.scalars().all():
            items.append(Resource(
                uri=f"case://{c.id}",
                name=f"Case: {c.id}",
                description=f"status={c.status} outcome={c.outcome}",
                mimeType="application/json",
            ))
    return items


@server.read_resource()
async def read_resource(uri: str) -> str:
    s = str(uri)
    if s.startswith("policy://"):
        policy_id = s[len("policy://"):]
        from app.api.policies import get_policy as get_policy_payload
        return json.dumps(get_policy_payload(policy_id), indent=2)
    if s.startswith("case://"):
        case_id = s[len("case://"):]
        async with session_scope() as session:
            case = await session.get(Case, case_id)
            if not case:
                return json.dumps({"error": "case not found"})
            return json.dumps({
                "case_id": case.id, "status": case.status, "outcome": case.outcome,
                "selected_policy_id": case.selected_policy_id,
                "policy_selection": case.policy_selection,
                "criteria_evaluation": case.criteria_evaluation,
                "determination": case.determination,
                "pas_response_bundle": case.pas_response_bundle,
            }, indent=2)
    return json.dumps({"error": f"unknown URI scheme: {s}"})


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@server.list_prompts()
async def list_prompts() -> list[Prompt]:
    return [
        Prompt(
            name="pa_review_summary",
            description="Summarize a PA case in 3 sentences for a medical director.",
            arguments=[
                PromptArgument(name="case_id", description="The case_id to summarize", required=True),
            ],
        ),
        Prompt(
            name="appeal_letter_draft",
            description="Draft an appeal letter for a denied case.",
            arguments=[
                PromptArgument(name="case_id", description="The denied case_id", required=True),
            ],
        ),
        Prompt(
            name="clinician_question",
            description="Generate a specific, single-response info request from a missing-info gap.",
            arguments=[
                PromptArgument(name="criterion_id", required=True),
                PromptArgument(name="gap_summary", required=True),
            ],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, Any]) -> GetPromptResult:
    if name == "pa_review_summary":
        case_id = arguments["case_id"]
        return GetPromptResult(
            description="Summarize PA case in 3 sentences.",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Read the case at case://{case_id}. Write a 3-sentence "
                            "summary for a medical director: (1) what was requested, "
                            "(2) what the determination is and why, (3) any missing "
                            "information needed to advance the case."
                        ),
                    ),
                )
            ],
        )
    if name == "appeal_letter_draft":
        case_id = arguments["case_id"]
        return GetPromptResult(
            description="Draft an appeal letter.",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Read case://{case_id}. Draft an appeal letter the "
                            "ordering provider could send to the payer that "
                            "addresses each criterion that fell short of `met` "
                            "and proposes specific corrective documentation."
                        ),
                    ),
                )
            ],
        )
    if name == "clinician_question":
        cid = arguments.get("criterion_id", "")
        gap = arguments.get("gap_summary", "")
        return GetPromptResult(
            description="Refine a missing-info request.",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(
                        type="text",
                        text=(
                            f"Refine this missing-info gap into a single-response, "
                            f"actionable clinician question. criterion={cid}; gap={gap}"
                        ),
                    ),
                )
            ],
        )
    return GetPromptResult(description="unknown prompt", messages=[])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    configure_logging()
    await init_db()
    # Eager-load policies (citation verification fires here)
    get_registry()
    log.info("mcp.server.start", transport="stdio")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
