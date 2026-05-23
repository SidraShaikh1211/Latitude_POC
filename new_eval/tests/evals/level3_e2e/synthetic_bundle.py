"""Helper to build a Da Vinci PAS Claim Bundle from a synthetic L3 case.

We wrap a hand-authored narrative (H&P-style text) into a real PDF using
PyMuPDF, base64-encode it, and emit a Bundle that the orchestrator can
consume exactly like the Smith fixture. This means every L3 synthetic case
exercises the full pipeline including PDF extraction + LLM intake — not
just adjudication.

Used by `test_l3_synthetic.py` to parametrize across multiple synthetic
cases without authoring 12 separate 3 MB JSON fixtures.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Literal

import pymupdf


# ---------------------------------------------------------------------------
# Case spec
# ---------------------------------------------------------------------------

Outcome = Literal["approve", "deny", "pend", "needs_human_review"]
Domain = Literal["esi", "zepbound", "adenomyosis"]


@dataclass
class L3SyntheticCase:
    """A synthetic end-to-end PA case. The narrative is hand-authored text;
    everything else is structured."""

    id: str
    domain: Domain

    # Patient
    family: str
    given: str
    birth_date: str       # YYYY-MM-DD
    gender: str           # "male"|"female"
    state: str            # two-letter state code
    mrn: str

    # Coverage
    payer_id: str         # matches policy.payer_id
    payer_display: str
    lob: str              # "Medicaid" | "Medicare-Advantage" | etc.
    member_id: str

    # Service requested
    cpt_code: str
    service_display: str
    request_category: str # "procedural" | "pharmacy" | "surgical"
    icd10_codes: list[str]
    service_date: str = "2026-04-08"

    # Narrative (the H&P-style text rendered to PDF)
    narrative: str = ""

    # Expected outcome
    expected_outcome: Outcome = "pend"
    # If outcome=="pend", reviewer narrative + missing_info should mention
    # at least one of these keywords (case-insensitive).
    expected_info_keywords: list[str] = field(default_factory=list)
    # If outcome=="deny", the rationale should reference one of these.
    expected_deny_keywords: list[str] = field(default_factory=list)

    # Optional: prior procedures (for repeat-branch testing)
    prior_procedures: list[dict] = field(default_factory=list)

    notes: str = ""  # rationale for inclusion in eval suite


# ---------------------------------------------------------------------------
# PDF rendering
# ---------------------------------------------------------------------------

def text_to_pdf_bytes(narrative: str) -> bytes:
    """Render a narrative string as a multi-page PDF. Splits on paragraph
    boundaries so each page is a clean chunk that comfortably fits, then uses
    PyMuPDF's insert_textbox to lay it out."""
    doc = pymupdf.open()
    # Letter size 612 x 792 pts; margins 60 pts
    rect = pymupdf.Rect(60, 60, 552, 732)
    fontsize = 10
    # Empirically: a Letter page at 10pt fits ~2000-2500 chars of prose
    # (including newlines). Stay conservative to avoid silent truncation.
    target_chars_per_page = 2000

    # Split on blank-line paragraph boundaries; then re-pack greedily.
    paragraphs = [p.strip() for p in narrative.strip().split("\n\n") if p.strip()]
    pages: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for para in paragraphs:
        plen = len(para) + 2  # +2 for the \n\n we'll re-insert
        if buf and buf_len + plen > target_chars_per_page:
            pages.append("\n\n".join(buf))
            buf = [para]
            buf_len = plen
        else:
            buf.append(para)
            buf_len += plen
    if buf:
        pages.append("\n\n".join(buf))

    for chunk in pages:
        page = doc.new_page(width=612, height=792)
        ret = page.insert_textbox(
            rect, chunk,
            fontsize=fontsize, fontname="helv",
            align=pymupdf.TEXT_ALIGN_LEFT,
        )
        # If even our packed chunk overflowed (rare for prose), shrink fontsize
        # and retry on the SAME page so we don't lose content.
        if ret < 0:
            # Clear the page by deleting + recreating, then retry at smaller font.
            doc.delete_page(-1)
            page = doc.new_page(width=612, height=792)
            page.insert_textbox(
                rect, chunk,
                fontsize=8, fontname="helv",
                align=pymupdf.TEXT_ALIGN_LEFT,
            )

    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# PAS Bundle builder
# ---------------------------------------------------------------------------

def build_pas_bundle(case: L3SyntheticCase) -> dict:
    """Wrap a synthetic case into a Da Vinci PAS Claim Bundle dict that the
    orchestrator can consume directly."""
    cid = case.id
    pid = f"patient-{cid}"
    cov = f"coverage-{cid}"
    sr_id = f"sr-{cid}"
    claim_id = f"claim-{cid}"
    org_payer = f"org-payer-{cid}"
    org_provider = f"org-provider-{cid}"
    doc_id = f"doc-{cid}"
    bin_id = f"binary-{cid}"

    pdf_bytes = text_to_pdf_bytes(case.narrative)
    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    # Determine claim type from request_category
    claim_type = {
        "procedural": "professional",
        "surgical": "institutional",
        "pharmacy": "pharmacy",
    }.get(case.request_category, "professional")

    entries: list[dict] = [
        # Claim
        {
            "fullUrl": f"urn:uuid:{claim_id}",
            "resource": {
                "resourceType": "Claim",
                "id": claim_id,
                "status": "active",
                "type": {
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/claim-type",
                        "code": claim_type,
                    }]
                },
                "use": "preauthorization",
                "patient": {"reference": f"Patient/{pid}"},
                "created": f"{case.service_date}T09:00:00Z",
                "insurer": {"reference": f"Organization/{org_payer}"},
                "provider": {"reference": f"Organization/{org_provider}"},
                "priority": {"coding": [{"code": "normal"}]},
                "insurance": [{
                    "sequence": 1,
                    "focal": True,
                    "coverage": {"reference": f"Coverage/{cov}"},
                }],
                "diagnosis": [
                    {
                        "sequence": i + 1,
                        "diagnosisCodeableConcept": {
                            "coding": [{
                                "system": "http://hl7.org/fhir/sid/icd-10-cm",
                                "code": code,
                            }]
                        },
                    }
                    for i, code in enumerate(case.icd10_codes)
                ],
                "item": [{
                    "sequence": 1,
                    "productOrService": {
                        "coding": [{
                            "system": "http://www.ama-assn.org/go/cpt",
                            "code": case.cpt_code,
                            "display": case.service_display,
                        }],
                        "text": case.service_display,
                    },
                    "category": {
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/ex-claimitemcategory",
                            "code": case.request_category,
                        }],
                        "text": case.request_category,
                    },
                }],
                "supportingInfo": [{
                    "sequence": 1,
                    "category": {"coding": [{"code": "patient-info"}]},
                    "valueReference": {"reference": f"DocumentReference/{doc_id}"},
                }],
            },
        },
        # Patient
        {
            "fullUrl": f"urn:uuid:{pid}",
            "resource": {
                "resourceType": "Patient",
                "id": pid,
                "name": [{"family": case.family, "given": [case.given]}],
                "gender": case.gender,
                "birthDate": case.birth_date,
                "identifier": [{
                    "system": "urn:oid:1.2.3.4.5.6.7.8.9",
                    "value": case.mrn,
                }],
                "address": [{
                    "use": "home",
                    "state": case.state,
                    "country": "US",
                }],
            },
        },
        # Coverage
        {
            "fullUrl": f"urn:uuid:{cov}",
            "resource": {
                "resourceType": "Coverage",
                "id": cov,
                "status": "active",
                "subscriberId": case.member_id,
                "beneficiary": {"reference": f"Patient/{pid}"},
                "payor": [{
                    "reference": f"Organization/{org_payer}",
                    "display": case.payer_display,
                    "identifier": {
                        "system": "urn:oid:2.16.840.1.113883.3.7204",
                        "value": case.payer_id,
                    },
                }],
                "type": {
                    "coding": [{
                        "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                        "code": case.lob,
                        "display": case.lob,
                    }],
                    "text": case.lob,
                },
            },
        },
        # ServiceRequest
        {
            "fullUrl": f"urn:uuid:{sr_id}",
            "resource": {
                "resourceType": "ServiceRequest",
                "id": sr_id,
                "status": "active",
                "intent": "order",
                "category": [{
                    "coding": [{
                        "system": "http://snomed.info/sct",
                        "code": "387713003",
                        "display": case.request_category.capitalize(),
                    }],
                    "text": case.request_category,
                }],
                "code": {
                    "coding": [{
                        "system": "http://www.ama-assn.org/go/cpt",
                        "code": case.cpt_code,
                        "display": case.service_display,
                    }],
                    "text": case.service_display,
                },
                "subject": {"reference": f"Patient/{pid}"},
                "authoredOn": f"{case.service_date}T09:00:00Z",
                "reasonCode": [
                    {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10-cm",
                                 "code": code}]}
                    for code in case.icd10_codes
                ],
            },
        },
        # Organization (payer)
        {
            "fullUrl": f"urn:uuid:{org_payer}",
            "resource": {
                "resourceType": "Organization",
                "id": org_payer,
                "name": case.payer_display,
                "identifier": [{
                    "system": "urn:oid:2.16.840.1.113883.3.7204",
                    "value": case.payer_id,
                }],
            },
        },
        # Organization (provider)
        {
            "fullUrl": f"urn:uuid:{org_provider}",
            "resource": {
                "resourceType": "Organization",
                "id": org_provider,
                "name": "Synthetic Provider Clinic",
            },
        },
        # DocumentReference
        {
            "fullUrl": f"urn:uuid:{doc_id}",
            "resource": {
                "resourceType": "DocumentReference",
                "id": doc_id,
                "status": "current",
                "type": {
                    "coding": [{
                        "system": "http://loinc.org",
                        "code": "11506-3",
                        "display": "Progress note",
                    }],
                    "text": "Clinical documentation",
                },
                "subject": {"reference": f"Patient/{pid}"},
                "date": f"{case.service_date}T09:00:00Z",
                "content": [{
                    "attachment": {
                        "contentType": "application/pdf",
                        "url": f"Binary/{bin_id}",
                        "title": f"{case.id} clinical narrative",
                    }
                }],
            },
        },
        # Binary (the synthetic PDF)
        {
            "fullUrl": f"urn:uuid:{bin_id}",
            "resource": {
                "resourceType": "Binary",
                "id": bin_id,
                "contentType": "application/pdf",
                "data": pdf_b64,
            },
        },
    ]

    # Append prior procedures (for repeat-branch testing)
    for i, proc in enumerate(case.prior_procedures):
        proc_id = f"prior-procedure-{cid}-{i}"
        entries.append({
            "fullUrl": f"urn:uuid:{proc_id}",
            "resource": {
                "resourceType": "Procedure",
                "id": proc_id,
                "status": "completed",
                "subject": {"reference": f"Patient/{pid}"},
                **proc,
            },
        })

    return {
        "resourceType": "Bundle",
        "id": f"pas-claim-{cid}",
        "type": "collection",
        "timestamp": f"{case.service_date}T09:00:00Z",
        "entry": entries,
    }
