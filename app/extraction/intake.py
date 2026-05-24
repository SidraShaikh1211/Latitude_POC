"""Intake pipeline: clinical PDF → FHIR-shaped resources with citations.

One Claude structured-output call per document. The model returns a
Pydantic-validated `ExtractedFacts` payload; every citation is then
substring-verified against the source PDF. Resources with failed citations
are dropped (logged + counted, not silently passed through).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from app.extraction.pdf import ExtractedDocument, extract_pdf
from app.llm.citation_verify import verify_substring
from app.llm.client import StructuredResult, Usage, get_client


log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Schema returned by Claude (Pydantic-validated)
# ---------------------------------------------------------------------------

class Citation(BaseModel):
    document_id: str
    page: int = Field(ge=1)
    section: str | None = None
    quote: str = Field(min_length=3)
    extraction_confidence: float = Field(ge=0.0, le=1.0, default=0.8)


class _BaseResource(BaseModel):
    resource_type: str
    id: str | None = None
    citations: list[Citation] = Field(default_factory=list)


class ExtractedPatient(_BaseResource):
    resource_type: Literal["Patient"] = "Patient"
    family: str | None = None
    given: str | None = None
    birth_date: str | None = None
    gender: str | None = None
    mrn: str | None = None


class ExtractedCondition(_BaseResource):
    resource_type: Literal["Condition"] = "Condition"
    icd10_code: str | None = None
    display: str
    clinical_status: str | None = None
    onset_date: str | None = None
    note: str | None = None


class ExtractedObservation(_BaseResource):
    resource_type: Literal["Observation"] = "Observation"
    code_display: str
    value_string: str | None = None
    value_quantity: float | None = None
    value_unit: str | None = None
    effective_date: str | None = None
    body_site: str | None = None
    category: str | None = None  # e.g., "pain-score", "exam-finding", "imaging"


class ExtractedMedication(_BaseResource):
    resource_type: Literal["MedicationRequest", "MedicationStatement"]
    medication_name: str
    dose: str | None = None
    frequency: str | None = None
    status: str | None = None
    start_date: str | None = None
    stop_date: str | None = None


class ExtractedProcedure(_BaseResource):
    resource_type: Literal["Procedure"] = "Procedure"
    cpt_code: str | None = None
    display: str
    status: str = "completed"
    performed_date: str | None = None
    body_site: str | None = None
    notes: str | None = None


class ExtractedAllergy(_BaseResource):
    resource_type: Literal["AllergyIntolerance"] = "AllergyIntolerance"
    substance: str
    reaction: str | None = None
    criticality: str | None = None


class ExtractedDiagnosticReport(_BaseResource):
    resource_type: Literal["DiagnosticReport"] = "DiagnosticReport"
    modality: str  # "MRI", "CT", "X-ray", "EMG/NCS"
    body_site: str
    findings: str
    performed_date: str | None = None


class ExtractedFacts(BaseModel):
    """The Pydantic schema Claude fills in via tool-use structured output."""

    patient: ExtractedPatient | None = None
    conditions: list[ExtractedCondition] = Field(default_factory=list)
    observations: list[ExtractedObservation] = Field(default_factory=list)
    medications: list[ExtractedMedication] = Field(default_factory=list)
    procedures: list[ExtractedProcedure] = Field(default_factory=list)
    allergies: list[ExtractedAllergy] = Field(default_factory=list)
    diagnostic_reports: list[ExtractedDiagnosticReport] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

@dataclass
class IntakeResult:
    document: ExtractedDocument
    facts: ExtractedFacts
    citation_passed: int
    citation_failed: int
    dropped_resources: list[str]
    usage: Usage

    @property
    def citation_pass_rate(self) -> float:
        total = self.citation_passed + self.citation_failed
        return 1.0 if total == 0 else self.citation_passed / total


def _load_skill(name: str) -> str:
    skill_path = Path(__file__).resolve().parent.parent.parent / "skills" / name / "SKILL.md"
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill not found: {skill_path}")
    return skill_path.read_text()


def _build_document_text(doc: ExtractedDocument, max_chars: int = 60000) -> str:
    """Build the user payload: per-page text with explicit page markers.

    The page markers (`--- PAGE n ---`) are how Claude knows which page to
    cite. We use the *normalized* text so citations substring-match later.
    """
    parts: list[str] = []
    used = 0
    for pt in doc.pages:
        header = f"\n--- PAGE {pt.page} ---\n"
        body = pt.normalized
        chunk = header + body
        if used + len(chunk) > max_chars:
            parts.append(header + body[: max_chars - used - len(header)])
            break
        parts.append(chunk)
        used += len(chunk)
    return "".join(parts)


def _all_resources(facts: ExtractedFacts) -> Iterable[_BaseResource]:
    if facts.patient is not None:
        yield facts.patient
    yield from facts.conditions
    yield from facts.observations
    yield from facts.medications
    yield from facts.procedures
    yield from facts.allergies
    yield from facts.diagnostic_reports


def _drop_resource(facts: ExtractedFacts, target: _BaseResource) -> None:
    """Remove a single dropped resource from its bucket."""
    if facts.patient is target:
        facts.patient = None
        return
    for bucket_name in (
        "conditions", "observations", "medications",
        "procedures", "allergies", "diagnostic_reports",
    ):
        bucket = getattr(facts, bucket_name)
        if target in bucket:
            bucket.remove(target)
            return


async def run_intake(
    document: ExtractedDocument,
    *,
    additional_context: str | None = None,
) -> IntakeResult:
    """Extract FHIR-shaped resources from a clinical document.

    Returns an `IntakeResult` carrying the validated `ExtractedFacts`, the
    citation pass rate, and the LLM usage."""
    system = _load_skill("pa-intake")

    doc_text = _build_document_text(document)
    user_parts: list[str] = []
    if additional_context:
        user_parts.append(additional_context)
    user_parts.append(f"document_id: {document.document_id}")
    user_parts.append(f"page_count: {document.page_count}")
    user_parts.append("Document text follows, with explicit `--- PAGE n ---` markers between pages.")
    user_parts.append(doc_text)
    user = "\n\n".join(user_parts)

    client = get_client()
    # Intake regularly emits 4-8K of structured FHIR for a multi-page PDF, which
    # easily blows past the client-default 60s per-request timeout and aborts
    # silently. Give it 5 minutes of headroom — well inside the orchestrator's
    # 600s pipeline deadline but enough that real PDFs finish.
    result: StructuredResult = await client.structured_output(
        system=system,
        user=user,
        schema=ExtractedFacts,
        max_tokens=8000,
        timeout=300.0,
    )
    facts: ExtractedFacts = result.parsed  # type: ignore[assignment]

    passed, failed, dropped = _verify_and_drop(facts, document)

    log.info(
        "intake.complete",
        document_id=document.document_id,
        citations_passed=passed,
        citations_failed=failed,
        dropped_resources=len(dropped),
        usage_in=result.usage.input_tokens,
        usage_out=result.usage.output_tokens,
        cache_read=result.usage.cache_read_tokens,
    )

    return IntakeResult(
        document=document,
        facts=facts,
        citation_passed=passed,
        citation_failed=failed,
        dropped_resources=dropped,
        usage=result.usage,
    )


def _verify_and_drop(
    facts: ExtractedFacts, document: ExtractedDocument
) -> tuple[int, int, list[str]]:
    """Verify every citation. Keep the resource if at least one citation
    survives; drop only the failed citations. A resource with NO verifiable
    citations is dropped entirely (we won't ground the adjudicator on
    un-sourced facts). Returns (passed, failed, dropped_resource_labels).

    Previously this dropped the *entire resource* on any failed citation,
    which lost verified facts whenever a single quote had a PDF whitespace
    glitch. Per-citation drop preserves the structured fact for downstream
    reasoning while still refusing to expose an unverifiable quote."""
    passed = 0
    failed = 0
    dropped: list[str] = []

    for resource in list(_all_resources(facts)):
        verified_citations: list[Citation] = []
        for cit in resource.citations:
            pt = document.page(cit.page)
            if pt is None:
                failed += 1
                log.warning(
                    "citation_failed",
                    resource_type=resource.resource_type,
                    page=cit.page,
                    quote=cit.quote[:80],
                    diagnostic=f"page {cit.page} out of range (1..{document.page_count})",
                )
                continue
            check = verify_substring(cit.quote, pt.normalized)
            if check.found:
                passed += 1
                verified_citations.append(cit)
            else:
                failed += 1
                log.warning(
                    "citation_failed",
                    resource_type=resource.resource_type,
                    page=cit.page,
                    quote=cit.quote[:80],
                    diagnostic=check.diagnostic,
                )

        if verified_citations:
            resource.citations = verified_citations
        else:
            dropped.append(
                f"{resource.resource_type}:{getattr(resource, 'display', '') or getattr(resource, 'medication_name', '') or resource.id or '?'}"
            )
            _drop_resource(facts, resource)

    return passed, failed, dropped


async def intake_from_path(pdf_path: str | Path, document_id: str | None = None) -> IntakeResult:
    """Convenience entry point: extract a PDF from disk and run intake."""
    doc = extract_pdf(pdf_path, document_id=document_id)
    return await run_intake(doc)
