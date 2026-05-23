"""Metadata extractor — pre-intake stage for the doctor PDF-only flow.

Reads a clinical PDF and produces a SubmissionData (the input to
build_pas_bundle). This is what the doctor's EHR would do automatically:
take the patient's chart + the order and assemble the wire-format
submission. Here, we use Claude to infer the structured fields from the
unstructured PDF — including CPT codes inferred from clinical context
when the doctor hasn't written them explicitly.

Distinct from the existing `intake.py`:
  - intake.py runs AFTER the Bundle is built; extracts FHIR resources
    (Conditions, Observations, etc.) with verbatim citations
  - metadata.py runs BEFORE the Bundle is built; extracts SubmissionData
    (CPT, ICD-10, payer, member ID, DOB) for assembly

The two could be merged but kept separate for clarity and so the metadata
extraction can fail fast (before any heavier intake/adjudication cost).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from app.extraction.pdf import ExtractedDocument
from app.llm.client import StructuredResult, Usage, get_client
from app.pas.bundle_constructor import ICD10Code, SubmissionData


log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Pydantic schemas Claude fills in
# ---------------------------------------------------------------------------


class PatientFields(BaseModel):
    patient_given: str | None = None
    patient_family: str | None = None
    patient_dob: str | None = Field(None, description="ISO date YYYY-MM-DD")
    patient_gender: Literal["male", "female", "other", "unknown"] = "unknown"
    patient_state: str | None = Field(None, description="2-letter US state code")


class CoverageFields(BaseModel):
    payer_id: str = Field(..., description="lowercase short id, e.g., 'molina'")
    payer_display: str | None = None
    member_id: str | None = None
    line_of_business: Literal["medicaid", "medicare-advantage", "commercial"] = "medicaid"
    plan_name: str | None = None


class _ICD10(BaseModel):
    code: str
    display: str
    kind: Literal["primary", "secondary"] = "secondary"


class ServiceRequestFields(BaseModel):
    cpt_code: str
    cpt_display: str | None = None
    service_date: str = Field(..., description="ISO date YYYY-MM-DD")
    icd10_codes: list[_ICD10] = Field(default_factory=list)
    body_site_display: str | None = None


class ExtractedMetadata(BaseModel):
    patient: PatientFields
    coverage: CoverageFields
    service_request: ServiceRequestFields
    extraction_notes: str = Field(
        default="", description="Short explanation of what was explicit vs inferred"
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="Field names the extractor could not fill from the document",
    )


# ---------------------------------------------------------------------------
# Result wrapper
# ---------------------------------------------------------------------------


@dataclass
class MetadataResult:
    metadata: ExtractedMetadata
    submission: SubmissionData
    usage: Usage
    notes: str
    missing_fields: list[str]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


REQUIRED_FOR_SUBMISSION = {"cpt_code", "service_date", "payer_id"}


def _load_skill() -> str:
    path = Path(__file__).resolve().parent.parent.parent / "skills" / "pa-metadata-extractor" / "SKILL.md"
    return path.read_text()


def _build_document_payload(doc: ExtractedDocument, max_chars: int = 60000) -> str:
    """Assemble the user payload: per-page text with explicit page markers."""
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


async def extract_submission_metadata(
    doc: ExtractedDocument,
    *,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> MetadataResult:
    """Run the metadata extractor on a clinical PDF.

    Returns the structured `ExtractedMetadata` AND a ready-to-use
    `SubmissionData` (with PDF bytes attached) that can be handed directly
    to `build_pas_bundle()`.
    """
    system = _load_skill()
    doc_text = _build_document_payload(doc)
    user = (
        "Extract the structured submission metadata from this clinical PDF. "
        "The doctor uploaded the PDF without filling any form fields — your "
        "extraction is the only source of patient + service + coverage info. "
        "Infer CPT codes from clinical context when not explicit. Return "
        "ExtractedMetadata via the `return_extractedmetadata` tool.\n\n"
        f"document_id: {doc.document_id}\n"
        f"page_count: {doc.page_count}\n"
        "Document text follows, with `--- PAGE n ---` markers between pages.\n"
        + doc_text
    )

    client = get_client()
    result: StructuredResult = await client.structured_output(
        system=system,
        user=user,
        schema=ExtractedMetadata,
        max_tokens=4096,
    )
    meta: ExtractedMetadata = result.parsed  # type: ignore[assignment]

    submission = _to_submission_data(meta, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename)

    log.info(
        "metadata_extractor.done",
        document_id=doc.document_id,
        cpt=meta.service_request.cpt_code,
        primary_icd10=_primary_icd10(meta),
        payer=meta.coverage.payer_id,
        missing_fields=meta.missing_fields,
        usage_in=result.usage.input_tokens,
        usage_out=result.usage.output_tokens,
        cache_read=result.usage.cache_read_tokens,
    )

    return MetadataResult(
        metadata=meta,
        submission=submission,
        usage=result.usage,
        notes=meta.extraction_notes,
        missing_fields=meta.missing_fields,
    )


def _primary_icd10(meta: ExtractedMetadata) -> str | None:
    for c in meta.service_request.icd10_codes:
        if c.kind == "primary":
            return c.code
    return meta.service_request.icd10_codes[0].code if meta.service_request.icd10_codes else None


def _to_submission_data(
    meta: ExtractedMetadata, *, pdf_bytes: bytes, pdf_filename: str
) -> SubmissionData:
    """Coerce ExtractedMetadata + PDF bytes into a SubmissionData usable by
    build_pas_bundle. Applies sensible defaults when fields are missing."""
    from datetime import date

    p = meta.patient
    c = meta.coverage
    s = meta.service_request

    # Convert _ICD10 → ICD10Code; ensure exactly one primary
    icd10s = [ICD10Code(code=x.code, display=x.display, kind=x.kind) for x in s.icd10_codes]
    if icd10s and not any(x.kind == "primary" for x in icd10s):
        icd10s[0].kind = "primary"
    if not icd10s:
        # Bundle requires at least one diagnosis; use a placeholder
        icd10s = [ICD10Code(code="UNKNOWN", display="No diagnosis extracted", kind="primary")]

    return SubmissionData(
        patient_given=p.patient_given or "Unknown",
        patient_family=p.patient_family or "Patient",
        patient_dob=p.patient_dob or "1900-01-01",
        patient_gender=p.patient_gender,
        patient_state=p.patient_state or _infer_state_from_payer(c.payer_id),
        payer_id=c.payer_id or "molina",
        payer_display=c.payer_display or (c.payer_id or "molina").title(),
        member_id=c.member_id or f"UNKNOWN-MEMBER",
        line_of_business=c.line_of_business,
        plan_name=c.plan_name or f"{c.payer_id or 'molina'} (plan unspecified)",
        cpt_code=s.cpt_code,
        cpt_display=s.cpt_display or s.cpt_code,
        service_date=s.service_date or date.today().isoformat(),
        icd10_codes=icd10s,
        body_site_display=s.body_site_display or "",
        pdf_bytes=pdf_bytes,
        pdf_filename=pdf_filename,
    )


def _infer_state_from_payer(payer_id: str | None) -> str:
    """Sensible default when state isn't in the PDF. Falls back to NY for Molina."""
    if not payer_id:
        return "NY"
    if payer_id.lower() == "molina":
        return "NY"
    return "NY"


def missing_required_fields(meta: ExtractedMetadata) -> list[str]:
    """Return which REQUIRED fields are missing or placeholders."""
    missing = []
    if not meta.service_request.cpt_code or meta.service_request.cpt_code.upper() == "UNKNOWN":
        missing.append("cpt_code")
    if not meta.service_request.service_date:
        missing.append("service_date")
    if not meta.coverage.payer_id:
        missing.append("payer_id")
    return missing
