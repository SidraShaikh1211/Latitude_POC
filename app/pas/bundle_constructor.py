"""Generic Da Vinci PAS Bundle constructor.

Builds a complete PAS Claim Bundle from a SubmissionData payload — the
clinical metadata + the patient PDF. Used by:
  - scripts/seed_smith_case.py (Smith fixture generation)
  - app/api/doctor.py (POST /v1/doctor/submit — doctor-uploaded PDFs)

The Bundle shape and resource IDs are derived from the case_id so that
multiple submissions can coexist (resource IDs like `patient-doc-abc123`,
`claim-doc-abc123`, etc.).
"""

from __future__ import annotations

import base64
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from fhir.resources.R4B.bundle import Bundle

from app.pas.categorize import infer_request_category


# ---------------------------------------------------------------------------
# Input payload
# ---------------------------------------------------------------------------


@dataclass
class ICD10Code:
    code: str
    display: str
    kind: Literal["primary", "secondary"] = "secondary"


@dataclass
class SubmissionData:
    """Everything needed to assemble a PAS Bundle. Mirrors the form fields
    a doctor would fill in their EHR."""

    # --- Patient ---
    patient_given: str
    patient_family: str
    patient_dob: str           # ISO date "YYYY-MM-DD"
    patient_gender: str        # "male" | "female" | "other"
    patient_state: str         # 2-letter, e.g. "NY"

    # --- Coverage ---
    payer_id: str              # e.g. "molina" (must match a loaded policy's payer_id)
    payer_display: str         # human-readable, e.g. "Molina Healthcare of New York"
    member_id: str
    line_of_business: str      # "medicaid" | "medicare-advantage" | "commercial"
    plan_name: str             # e.g. "Molina Medicaid NY" (state suffix matters for selector)

    # --- Service request ---
    cpt_code: str
    cpt_display: str
    service_date: str          # ISO date
    icd10_codes: list[ICD10Code]
    body_site_display: str     # e.g. "Lumbar — L4/5 vs L5/S1"

    # --- Documents ---
    pdf_bytes: bytes
    pdf_filename: str

    # --- Provider (defaults are POC-acceptable) ---
    provider_org_name: str = "Submitting Provider"
    practitioner_family: str = "Provider"
    practitioner_given: str = "Doctor"

    # --- Optional overrides ---
    submission_timestamp: str | None = None    # ISO datetime; defaults to now()
    doc_title: str | None = None               # defaults to pdf_filename


def _normalize_icd10(codes: list[ICD10Code] | list[dict]) -> list[ICD10Code]:
    out: list[ICD10Code] = []
    for c in codes:
        if isinstance(c, ICD10Code):
            out.append(c)
        elif isinstance(c, dict):
            out.append(ICD10Code(
                code=c["code"],
                display=c.get("display", c["code"]),
                kind=c.get("kind", "secondary"),
            ))
    return out


# ---------------------------------------------------------------------------
# Builders (per-resource)
# ---------------------------------------------------------------------------


def _patient(sub: SubmissionData, ids: "_Ids") -> dict:
    return {
        "resourceType": "Patient",
        "id": ids.patient,
        "name": [{"family": sub.patient_family, "given": [sub.patient_given]}],
        "gender": sub.patient_gender,
        "birthDate": sub.patient_dob,
        "address": [{
            "use": "home",
            "state": sub.patient_state,
            "country": "US",
        }],
    }


def _coverage(sub: SubmissionData, ids: "_Ids") -> dict:
    lob_code_map = {
        "medicaid": "Medicaid",
        "medicare-advantage": "Medicare",
        "commercial": "Commercial",
    }
    lob_code = lob_code_map.get(sub.line_of_business, "Medicaid")
    return {
        "resourceType": "Coverage",
        "id": ids.coverage,
        "status": "active",
        "subscriberId": sub.member_id,
        "beneficiary": {"reference": f"Patient/{ids.patient}"},
        "payor": [{
            "reference": f"Organization/{ids.org_payer}",
            "display": sub.payer_display,
            "identifier": {
                "system": "urn:oid:2.16.840.1.113883.3.7204",
                "value": sub.payer_id,
            },
        }],
        "type": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                "code": lob_code,
                "display": lob_code,
            }],
            "text": lob_code,
        },
        "class": [{
            "type": {"coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/coverage-class",
                "code": "plan",
            }]},
            "value": sub.plan_name,
            "name": sub.plan_name,
        }],
        "period": {"start": "2024-01-01"},
    }


def _org_payer(sub: SubmissionData, ids: "_Ids") -> dict:
    return {
        "resourceType": "Organization",
        "id": ids.org_payer,
        "name": sub.payer_display,
        "identifier": [{
            "system": "urn:oid:2.16.840.1.113883.3.7204",
            "value": sub.payer_id,
        }],
        "type": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/organization-type",
                "code": "ins",
                "display": "Insurance Company",
            }],
        }],
    }


def _org_provider(sub: SubmissionData, ids: "_Ids") -> dict:
    return {
        "resourceType": "Organization",
        "id": ids.org_provider,
        "name": sub.provider_org_name,
        "type": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/organization-type",
                "code": "prov",
                "display": "Healthcare Provider",
            }],
        }],
    }


def _practitioner(sub: SubmissionData, ids: "_Ids") -> dict:
    return {
        "resourceType": "Practitioner",
        "id": ids.practitioner,
        "name": [{"family": sub.practitioner_family, "given": [sub.practitioner_given]}],
        "qualification": [{"code": {"text": "MD"}}],
    }


def _service_request(sub: SubmissionData, ids: "_Ids") -> dict:
    category_text = infer_request_category(sub.cpt_code)
    # Map our category to a SNOMED code so the FHIR shape stays valid.
    snomed = {
        "surgical":   ("387713003", "Surgical procedure"),
        "procedural": ("103693007", "Diagnostic procedure"),
        "pharmacy":   ("440655000", "Outpatient pharmacy service"),
    }.get(category_text, ("103693007", "Diagnostic procedure"))
    return {
        "resourceType": "ServiceRequest",
        "id": ids.service_request,
        "status": "active",
        "intent": "order",
        "category": [{
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": snomed[0],
                "display": snomed[1],
            }],
            "text": category_text,
        }],
        "code": {
            "coding": [{
                "system": "http://www.ama-assn.org/go/cpt",
                "code": sub.cpt_code,
                "display": sub.cpt_display,
            }],
            "text": sub.cpt_display,
        },
        "subject": {"reference": f"Patient/{ids.patient}"},
        "occurrenceDateTime": sub.service_date,
        "requester": {"reference": f"Practitioner/{ids.practitioner}"},
        "performer": [{"reference": f"Organization/{ids.org_provider}"}],
        "bodySite": [{
            "text": sub.body_site_display,
        }],
        "locationCode": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v3-RoleCode",
                "code": "OUTPHARM",
                "display": "outpatient",
            }],
            "text": "outpatient",
        }],
    }


def _document_reference_and_binary(
    sub: SubmissionData, ids: "_Ids"
) -> tuple[dict, dict]:
    b64 = base64.b64encode(sub.pdf_bytes).decode("ascii")
    binary = {
        "resourceType": "Binary",
        "id": ids.binary,
        "contentType": "application/pdf",
        "data": b64,
    }
    doc_title = sub.doc_title or sub.pdf_filename
    doc_ref = {
        "resourceType": "DocumentReference",
        "id": ids.document,
        "status": "current",
        "type": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "11506-3",
                "display": "Progress note",
            }],
            "text": "Clinical documentation",
        },
        "category": [{
            "coding": [{
                "system": "http://hl7.org/fhir/us/core/ValueSet/us-core-documentreference-category",
                "code": "clinical-note",
            }],
        }],
        "subject": {"reference": f"Patient/{ids.patient}"},
        "date": sub.submission_timestamp or _utc_now_iso(),
        "author": [{"reference": f"Organization/{ids.org_provider}"}],
        "content": [{
            "attachment": {
                "contentType": "application/pdf",
                "url": f"Binary/{ids.binary}",
                "title": doc_title,
                "size": len(sub.pdf_bytes),
            },
        }],
    }
    return doc_ref, binary


def _claim(sub: SubmissionData, ids: "_Ids") -> dict:
    icd10s = _normalize_icd10(sub.icd10_codes)
    diagnosis_entries = []
    for i, dx in enumerate(icd10s, start=1):
        diagnosis_entries.append({
            "sequence": i,
            "diagnosisCodeableConcept": {
                "coding": [{
                    "system": "http://hl7.org/fhir/sid/icd-10-cm",
                    "code": dx.code,
                    "display": dx.display,
                }],
            },
            "type": [{
                "coding": [{
                    "system": "http://terminology.hl7.org/CodeSystem/ex-diagnosistype",
                    "code": "principal" if dx.kind == "primary" else "secondary",
                }],
            }],
        })
    diagnosis_seq = list(range(1, len(icd10s) + 1)) or [1]

    return {
        "resourceType": "Claim",
        "id": ids.claim,
        "status": "active",
        "type": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                "code": "professional",
            }],
        },
        "use": "preauthorization",
        "patient": {"reference": f"Patient/{ids.patient}"},
        "created": sub.submission_timestamp or _utc_now_iso(),
        "insurer": {"reference": f"Organization/{ids.org_payer}"},
        "provider": {"reference": f"Organization/{ids.org_provider}"},
        "priority": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/processpriority",
                "code": "normal",
            }],
        },
        "insurance": [{
            "sequence": 1,
            "focal": True,
            "coverage": {"reference": f"Coverage/{ids.coverage}"},
        }],
        "diagnosis": diagnosis_entries,
        "item": [{
            "sequence": 1,
            "productOrService": {
                "coding": [{
                    "system": "http://www.ama-assn.org/go/cpt",
                    "code": sub.cpt_code,
                    "display": sub.cpt_display,
                }],
            },
            "servicedDate": sub.service_date,
            "diagnosisSequence": diagnosis_seq,
            "bodySite": {
                "text": sub.body_site_display,
            },
        }],
        "supportingInfo": [{
            "sequence": 1,
            "category": {
                "coding": [{
                    "system": "http://hl7.org/fhir/us/davinci-pas/CodeSystem/PASSupportingInfoType",
                    "code": "patientEvent",
                }],
                "text": "Clinical documentation",
            },
            "valueReference": {"reference": f"DocumentReference/{ids.document}"},
        }],
    }


# ---------------------------------------------------------------------------
# Resource ID generator (one per case_id)
# ---------------------------------------------------------------------------


@dataclass
class _Ids:
    case_id: str
    patient: str
    coverage: str
    claim: str
    service_request: str
    document: str
    binary: str
    org_payer: str
    org_provider: str
    practitioner: str


def _ids_for(case_id: str) -> _Ids:
    """Per-case resource IDs derived from the case_id, so multiple submissions
    don't collide on resource IDs."""
    return _Ids(
        case_id=case_id,
        patient=f"patient-{case_id}",
        coverage=f"coverage-{case_id}",
        claim=f"claim-{case_id}",
        service_request=f"sr-{case_id}",
        document=f"doc-{case_id}",
        binary=f"binary-{case_id}",
        org_payer=f"org-payer-{case_id}",
        org_provider=f"org-provider-{case_id}",
        practitioner=f"practitioner-{case_id}",
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_pas_bundle(
    submission: SubmissionData,
    *,
    case_id: str | None = None,
) -> tuple[dict, str]:
    """Build a Da Vinci PAS Claim Bundle from a SubmissionData payload.

    Returns (bundle_dict, case_id). If `case_id` is None, a fresh one is
    generated.
    """
    case_id = case_id or f"sub-{uuid.uuid4().hex[:10]}"
    ids = _ids_for(case_id)

    patient = _patient(submission, ids)
    coverage = _coverage(submission, ids)
    org_payer = _org_payer(submission, ids)
    org_provider = _org_provider(submission, ids)
    practitioner = _practitioner(submission, ids)
    service_request = _service_request(submission, ids)
    doc_ref, binary = _document_reference_and_binary(submission, ids)
    claim = _claim(submission, ids)

    entries = [
        {"fullUrl": f"urn:uuid:{ids.claim}",           "resource": claim},
        {"fullUrl": f"urn:uuid:{ids.patient}",         "resource": patient},
        {"fullUrl": f"urn:uuid:{ids.coverage}",        "resource": coverage},
        {"fullUrl": f"urn:uuid:{ids.service_request}", "resource": service_request},
        {"fullUrl": f"urn:uuid:{ids.org_payer}",       "resource": org_payer},
        {"fullUrl": f"urn:uuid:{ids.org_provider}",    "resource": org_provider},
        {"fullUrl": f"urn:uuid:{ids.practitioner}",    "resource": practitioner},
        {"fullUrl": f"urn:uuid:{ids.document}",        "resource": doc_ref},
        {"fullUrl": f"urn:uuid:{ids.binary}",          "resource": binary},
    ]
    bundle_data = {
        "resourceType": "Bundle",
        "id": f"pas-bundle-{case_id}",
        "type": "collection",
        "timestamp": submission.submission_timestamp or _utc_now_iso(),
        "entry": entries,
    }
    # Validate R4 shape — raises if the Bundle is malformed
    Bundle.model_validate(bundle_data)
    return bundle_data, case_id


