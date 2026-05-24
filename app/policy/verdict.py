"""Per-criterion verdict schema.

Lives in its own module so both `adjudicator.py` (LLM agent loop) and
`deterministic_eval.py` (Python short-circuit) can produce CriterionVerdict
objects without an import cycle.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


Verdict = Literal["met", "not_met", "unclear", "not_documented"]


class PatientEvidence(BaseModel):
    fhir_resource_id: str | None = None
    fact_type: str | None = None
    value_summary: str
    document_id: str | None = None
    page: int | None = None
    quote: str | None = None
    verified: bool = False


class CriterionVerdict(BaseModel):
    criterion_id: str
    verdict: Verdict
    confidence: float = Field(ge=0.0, le=1.0)
    patient_evidence: list[PatientEvidence] = Field(default_factory=list)
    reasoning: str
    missing_info: list[str] = Field(default_factory=list)
