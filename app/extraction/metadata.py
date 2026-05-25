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
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from app.extraction import icd10_lookup as icd10
from app.extraction.pdf import ExtractedDocument
from app.llm.client import Usage, get_client
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


_ICD_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "icd10_lookup",
        "description": (
            "Confirm an ICD-10-CM code and return its official tabular "
            "description. MUST be called for every ICD-10 code you intend "
            "to emit, before emitting it. The returned `official_description` "
            "is the source of truth for the `display` field — copy it verbatim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Candidate ICD-10-CM code, e.g. 'M54.16' or 'N80.1'.",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "icd10_search_by_term",
        "description": (
            "Find candidate ICD-10-CM codes whose official descriptions "
            "contain a clinical term (e.g. 'endometriosis', 'type 2 diabetes'). "
            "Call this when the chart documents a condition by name without "
            "giving a literal code. Returns up to ~25 candidates ordered "
            "least-specific first; pick the most-specific code that fits "
            "the chart's documented body site / laterality / severity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "term": {
                    "type": "string",
                    "description": "Clinical term to search for, case-insensitive.",
                },
            },
            "required": ["term"],
        },
    },
]


async def extract_submission_metadata(
    doc: ExtractedDocument,
    *,
    pdf_bytes: bytes,
    pdf_filename: str,
) -> MetadataResult:
    """Run the metadata extractor on a clinical PDF.

    Uses an agent loop so Claude can call `icd10_lookup` / `icd10_search_by_term`
    to ground every ICD code against the official ICD-10-CM tabular list before
    emitting. Returns the structured `ExtractedMetadata` AND a ready-to-use
    `SubmissionData` (with PDF bytes attached) that can be handed directly
    to `build_pas_bundle()`.
    """
    system = _load_skill()
    doc_text = _build_document_payload(doc)
    user = (
        "Extract the structured submission metadata from this clinical PDF. "
        "The doctor uploaded the PDF without filling any form fields — your "
        "extraction is the only source of patient + service + coverage info. "
        "Infer CPT codes from clinical context when not explicit. For ICD-10 "
        "codes, follow the mandatory tool-use workflow in the system prompt: "
        "call `icd10_search_by_term` / `icd10_lookup` before emitting any "
        "code. Finalize by calling `return_extractedmetadata`.\n\n"
        f"document_id: {doc.document_id}\n"
        f"page_count: {doc.page_count}\n"
        "Document text follows, with `--- PAGE n ---` markers between pages.\n"
        + doc_text
    )

    return_tool = {
        "name": "return_extractedmetadata",
        "description": (
            "Submit the final ExtractedMetadata. Calling this ends the "
            "extraction loop. Every ICD-10 code included here must already "
            "have been confirmed via `icd10_lookup`."
        ),
        "input_schema": ExtractedMetadata.model_json_schema(),
    }
    tools = _ICD_TOOL_DEFINITIONS + [return_tool]

    captured: dict[str, Any] = {"payload": None}

    async def tool_handler(name: str, payload: dict) -> Any:
        if name == "return_extractedmetadata":
            captured["payload"] = payload
            return {"ok": True}
        if name == "icd10_lookup":
            return icd10.lookup(payload.get("code", ""))
        if name == "icd10_search_by_term":
            return icd10.search_by_term(payload.get("term", ""))
        return {"error": f"unknown tool {name!r}"}

    client = get_client()
    trace = await client.agent_loop(
        system=system,
        user=user,
        tools=tools,
        tool_handler=tool_handler,
        max_iterations=12,
        max_tokens=4096,
        cache_system=True,
        cache_tools=True,
        final_tool_name="return_extractedmetadata",
    )

    if captured["payload"] is None:
        raise RuntimeError(
            f"metadata extractor did not call return_extractedmetadata within "
            f"{trace.iterations} iterations (stop_reason={trace.stop_reason!r})"
        )

    meta: ExtractedMetadata = ExtractedMetadata.model_validate(captured["payload"])

    # Safety net: every emitted ICD must agree with the official tabular list.
    # If Claude paraphrased the display or emitted a bogus code, fix it now.
    _validate_and_correct_icd10(meta)

    submission = _to_submission_data(meta, pdf_bytes=pdf_bytes, pdf_filename=pdf_filename)

    log.info(
        "metadata_extractor.done",
        document_id=doc.document_id,
        cpt=meta.service_request.cpt_code,
        primary_icd10=_primary_icd10(meta),
        payer=meta.coverage.payer_id,
        missing_fields=meta.missing_fields,
        iterations=trace.iterations,
        tool_calls=len(trace.tool_calls),
        usage_in=trace.usage.input_tokens,
        usage_out=trace.usage.output_tokens,
        cache_read=trace.usage.cache_read_tokens,
    )

    return MetadataResult(
        metadata=meta,
        submission=submission,
        usage=trace.usage,
        notes=meta.extraction_notes,
        missing_fields=meta.missing_fields,
    )


def _normalize_display(s: str) -> str:
    """Lowercase + collapse punctuation/whitespace for tolerant comparison."""
    out = []
    prev_space = False
    for ch in s.lower():
        if ch.isalnum():
            out.append(ch)
            prev_space = False
        else:
            if not prev_space:
                out.append(" ")
                prev_space = True
    return "".join(out).strip()


def _validate_and_correct_icd10(meta: ExtractedMetadata) -> None:
    """Confirm every emitted ICD against the official ICD-10-CM tabular.

    - Invalid code → drop entry, append `icd10:{code}` to `missing_fields`.
    - Display doesn't match official text → overwrite display with the
      canonical description and record the correction in `extraction_notes`.

    Mutates `meta` in place. Lenient by design: the mandate in the prompt is
    the primary defense; this is just the safety net that ensures the bundle
    never ships a code/display pair that contradicts ICD-10-CM.
    """
    kept: list = []
    corrections: list[str] = []
    for entry in meta.service_request.icd10_codes:
        r = icd10.lookup(entry.code)
        if not r["valid"]:
            corrections.append(
                f"dropped ICD '{entry.code}' (not a valid ICD-10-CM code)"
            )
            field = f"icd10:{entry.code}"
            if field not in meta.missing_fields:
                meta.missing_fields.append(field)
            continue
        official = r["official_description"] or ""
        if _normalize_display(entry.display) != _normalize_display(official):
            corrections.append(
                f"corrected display for {entry.code}: "
                f"{entry.display!r} → {official!r}"
            )
            entry.display = official
        kept.append(entry)
    meta.service_request.icd10_codes = kept
    if corrections:
        note = " ICD-10 validator: " + "; ".join(corrections) + "."
        meta.extraction_notes = (meta.extraction_notes or "") + note


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
