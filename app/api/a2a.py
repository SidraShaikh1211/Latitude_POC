"""A2A Agent Card — the well-known endpoint advertising this service's
capabilities (spec §6.11). Optional / stretch."""

from fastapi import APIRouter

from app.settings import settings


router = APIRouter(tags=["a2a"])


@router.get("/.well-known/agent.json")
def agent_card() -> dict:
    return {
        "schemaVersion": "0.1",
        "name": "PA Prototype PA Prototype",
        "description": (
            "Payer-side prior-authorization decision support. Accepts Da Vinci "
            "PAS Claim Bundles, evaluates against policy criteria with citation-"
            "grounded reasoning, returns a PAS ClaimResponse Bundle."
        ),
        "version": "0.1.0",
        "model": settings.anthropic_model,
        "capabilities": [
            {
                "name": "evaluate_prior_auth",
                "description": "Evaluate a PAS Claim Bundle and return determination + ClaimResponse Bundle.",
                "input": "FHIR R4 Bundle (type=collection) containing Claim/Patient/Coverage/...",
                "output": "FHIR R4 Bundle containing ClaimResponse",
                "endpoints": [
                    {"protocol": "https", "path": "/fhir/Claim/$submit", "method": "POST"},
                    {"protocol": "mcp", "tool": "evaluate_prior_auth"},
                ],
            }
        ],
        "policies_loaded": [],
        "contacts": {
            "documentation": "/docs",
        },
    }
