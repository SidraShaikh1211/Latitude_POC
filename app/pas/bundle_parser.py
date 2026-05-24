"""Da Vinci PAS Bundle parser.

Consumes an inbound FHIR Bundle (the wire format a provider EHR sends to the
payer's PAS endpoint) and produces:
  - CaseContext: the metadata the Selector needs (CPT, ICD-10s, payer, LOB,
    state, age, service date, urgency, care setting, request category)
  - PatientFacts: the structured clinical evidence (current Conditions /
    Observations / Medications + prior Procedures + AllergyIntolerances) and
    document references.

Validation is R4 shape only — Da Vinci PAS IG profile validation is out of
scope for this prototype and documented as such.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from fhir.resources.R4B.bundle import Bundle
from fhir.resources.R4B.claim import Claim
from fhir.resources.R4B.coverage import Coverage
from fhir.resources.R4B.patient import Patient


class BundleParseError(Exception):
    pass


@dataclass
class CaseContext:
    cpt_code: str
    icd10_codes: list[str]
    payer_id: str
    line_of_business: str
    state: str
    patient_age: int
    service_date: date
    urgency: str = "standard"
    care_setting: str = "outpatient"
    request_category: str = "procedural"
    # ICD-10s the requested line item is *for* (Claim.item.diagnosisSequence
    # resolved against Claim.diagnosis). Falls back to `icd10_codes` when the
    # bundle does not link an item to specific diagnoses.
    requested_indication_icd10_codes: list[str] = field(default_factory=list)


@dataclass
class FactCollection:
    patient: dict | None = None
    coverage: dict | None = None
    claim: dict | None = None
    service_request: dict | None = None
    conditions: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    medication_requests: list[dict] = field(default_factory=list)
    medication_statements: list[dict] = field(default_factory=list)
    allergies: list[dict] = field(default_factory=list)
    procedures_prior: list[dict] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    binaries: dict[str, bytes] = field(default_factory=dict)


@dataclass
class ParsedBundle:
    context: CaseContext
    facts: FactCollection
    raw_bundle: dict


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def parse_pas_bundle(bundle_data: dict) -> ParsedBundle:
    # R4 shape validation; raises if Bundle is malformed.
    try:
        Bundle.model_validate(bundle_data)
    except Exception as e:
        raise BundleParseError(f"Bundle failed R4 shape validation: {e}") from e

    entries = bundle_data.get("entry") or []
    by_type: dict[str, list[dict]] = {}
    binaries: dict[str, bytes] = {}
    for entry in entries:
        resource = entry.get("resource") or {}
        rtype = resource.get("resourceType")
        if not rtype:
            continue
        by_type.setdefault(rtype, []).append(resource)
        if rtype == "Binary":
            bid = resource.get("id")
            data = resource.get("data")
            if bid and data:
                try:
                    binaries[bid] = base64.b64decode(data)
                except Exception:
                    pass

    claim = _exactly_one(by_type.get("Claim"), "Claim")
    if claim.get("use") != "preauthorization":
        raise BundleParseError(
            f"Claim.use must be 'preauthorization', got {claim.get('use')!r}"
        )

    coverage = _exactly_one(by_type.get("Coverage"), "Coverage")
    patient = _exactly_one(by_type.get("Patient"), "Patient")

    service_date = _parse_service_date(claim)
    cpt_code = _extract_primary_cpt(claim)
    dx_index = _build_diagnosis_index(claim)
    icd10_codes = list(dx_index.values())
    requested_indication_icd10_codes = _extract_requested_indication_codes(claim, dx_index)
    payer_id = _extract_payer_id(coverage)
    line_of_business = _extract_lob(coverage)
    state = _extract_state(coverage, patient)
    age = _compute_age(patient, service_date)
    urgency = _extract_urgency(claim, service_date)
    care_setting, request_category = _extract_setting_and_category(
        claim, by_type.get("ServiceRequest", [])
    )

    context = CaseContext(
        cpt_code=cpt_code,
        icd10_codes=icd10_codes,
        payer_id=payer_id,
        line_of_business=line_of_business,
        state=state,
        patient_age=age,
        service_date=service_date,
        urgency=urgency,
        care_setting=care_setting,
        request_category=request_category,
        requested_indication_icd10_codes=requested_indication_icd10_codes,
    )

    sr_list = by_type.get("ServiceRequest", [])
    service_request = sr_list[0] if sr_list else None

    facts = FactCollection(
        patient=patient,
        coverage=coverage,
        claim=claim,
        service_request=service_request,
        conditions=by_type.get("Condition", []),
        observations=by_type.get("Observation", []),
        medication_requests=by_type.get("MedicationRequest", []),
        medication_statements=by_type.get("MedicationStatement", []),
        allergies=by_type.get("AllergyIntolerance", []),
        procedures_prior=_filter_prior_procedures(by_type.get("Procedure", []), service_date),
        documents=by_type.get("DocumentReference", []),
        binaries=binaries,
    )

    return ParsedBundle(context=context, facts=facts, raw_bundle=bundle_data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _exactly_one(items: list[dict] | None, name: str) -> dict:
    items = items or []
    if not items:
        raise BundleParseError(f"Bundle missing required resource: {name}")
    if len(items) > 1:
        raise BundleParseError(f"Bundle has {len(items)} {name} resources; expected exactly 1")
    return items[0]


def _parse_service_date(claim: dict) -> date:
    # PAS Claim typically uses Claim.created (the submission date) and a separate
    # Claim.item[].serviced[x] for the requested service date. Use serviced if
    # present, otherwise fall back to created.
    items = claim.get("item") or []
    for item in items:
        sd = item.get("servicedDate")
        if sd:
            return date.fromisoformat(sd)
        period = item.get("servicedPeriod") or {}
        if period.get("start"):
            return date.fromisoformat(period["start"][:10])
    created = claim.get("created")
    if created:
        return date.fromisoformat(created[:10])
    return date.today()


def _extract_primary_cpt(claim: dict) -> str:
    items = claim.get("item") or []
    if not items:
        raise BundleParseError("Claim has no items; cannot determine CPT")
    coding = (items[0].get("productOrService") or {}).get("coding") or []
    for c in coding:
        if "cpt" in (c.get("system", "").lower()) or "ama-assn" in (c.get("system", "").lower()):
            return c.get("code", "")
    if coding:
        return coding[0].get("code", "")
    raise BundleParseError("Claim.item[0].productOrService has no coding")


def _build_diagnosis_index(claim: dict) -> dict[int, str]:
    """Map Claim.diagnosis[].sequence → ICD-10 code.

    When `sequence` is missing on an entry we synthesize a 1-based index in
    document order, matching FHIR's implicit sequence rule.
    """
    index: dict[int, str] = {}
    for i, dx in enumerate(claim.get("diagnosis") or [], start=1):
        seq = dx.get("sequence") or i
        coding = (dx.get("diagnosisCodeableConcept") or {}).get("coding") or []
        for c in coding:
            system = (c.get("system") or "").lower()
            if "icd-10" in system or "icd10" in system:
                code = c.get("code")
                if code:
                    index[int(seq)] = code
                    break
    return index


def _extract_requested_indication_codes(
    claim: dict, dx_index: dict[int, str]
) -> list[str]:
    """Resolve `Claim.item[0].diagnosisSequence` against the diagnosis index.

    The diagnosis-sequence link is the FHIR way to say "this line item is
    requested *for* these specific diagnoses, not the patient's full problem
    list." When the link is absent (legacy bundles or single-indication
    cases) we fall back to every ICD-10 the claim carries.
    """
    items = claim.get("item") or []
    if not items:
        return list(dx_index.values())
    seqs = items[0].get("diagnosisSequence") or []
    linked = [dx_index[int(s)] for s in seqs if int(s) in dx_index]
    if linked:
        return linked
    return list(dx_index.values())


def _extract_payer_id(coverage: dict) -> str:
    payor = coverage.get("payor") or []
    for p in payor:
        ident = p.get("identifier") or {}
        if ident.get("value"):
            return ident["value"].lower()
        if p.get("display"):
            return _slugify(p["display"]).split("-")[0]
    return ""


def _extract_lob(coverage: dict) -> str:
    cov_type = (coverage.get("type") or {}).get("coding") or []
    for c in cov_type:
        code = (c.get("code") or "").lower()
        if "medicaid" in code:
            return "medicaid"
        if "medicare" in code:
            return "medicare-advantage"
        if "commercial" in code or "ppo" in code or "hmo" in code:
            return "commercial"
    # Fall back to plan name
    for cls in coverage.get("class") or []:
        type_code = ((cls.get("type") or {}).get("coding") or [{}])[0].get("code", "").lower()
        if type_code == "plan":
            value = (cls.get("value") or "").lower()
            if "medicaid" in value:
                return "medicaid"
            if "medicare" in value:
                return "medicare-advantage"
            if "commercial" in value:
                return "commercial"
    return ""


def _extract_state(coverage: dict, patient: dict) -> str:
    # Try plan name first ("Molina Medicaid NY")
    for cls in coverage.get("class") or []:
        type_code = ((cls.get("type") or {}).get("coding") or [{}])[0].get("code", "").lower()
        if type_code == "plan":
            value = cls.get("value") or ""
            # Look for trailing 2-letter state code
            parts = value.strip().split()
            if parts and len(parts[-1]) == 2 and parts[-1].isupper():
                return parts[-1]
    # Try Patient.address
    for addr in patient.get("address") or []:
        st = addr.get("state")
        if st:
            return st[:2].upper() if len(st) > 2 else st.upper()
    return ""


def _compute_age(patient: dict, service_date: date) -> int:
    bd = patient.get("birthDate")
    if not bd:
        return -1
    birth = date.fromisoformat(bd)
    age = service_date.year - birth.year
    if (service_date.month, service_date.day) < (birth.month, birth.day):
        age -= 1
    return age


def _extract_urgency(claim: dict, service_date: date) -> str:
    priority = (claim.get("priority") or {}).get("coding") or []
    for c in priority:
        code = (c.get("code") or "").lower()
        if code in {"stat", "asap", "urgent"}:
            return "urgent"
    # Retrospective if claim was created after the service date
    created = claim.get("created")
    if created:
        try:
            created_d = date.fromisoformat(created[:10])
            if created_d > service_date:
                return "retrospective"
        except Exception:
            pass
    return "standard"


def _extract_setting_and_category(
    claim: dict, srs: list[dict]
) -> tuple[str, str]:
    # ServiceRequest.category gives us the request category; default to procedural.
    # Prefer category.text (set by our bundle constructor to one of
    # surgical / procedural / pharmacy) over the SNOMED code, which is a
    # categorical label that doesn't map 1:1 to our category vocabulary.
    allowed = {"pharmacy", "dme", "service", "procedural", "surgical"}
    category = "procedural"
    setting = "outpatient"
    for sr in srs:
        for cat in sr.get("category") or []:
            text = (cat.get("text") or "").strip().lower()
            if text in allowed:
                category = text
            for c in cat.get("coding") or []:
                code = (c.get("code") or "").lower()
                if code in allowed:
                    category = code
        loc = sr.get("locationCode") or []
        for l in loc:
            for c in l.get("coding") or []:
                code = (c.get("code") or "").lower()
                if "asc" in code or "ambulatory" in code:
                    setting = "ambulatory-surgery-center"
                elif "inpatient" in code:
                    setting = "inpatient"
                elif "office" in code:
                    setting = "office"
    # Claim.facility may also hint at setting
    return setting, category


def _filter_prior_procedures(procedures: list[dict], service_date: date) -> list[dict]:
    prior = []
    for p in procedures:
        if p.get("status") != "completed":
            continue
        performed = p.get("performedDateTime") or (p.get("performedPeriod") or {}).get("end")
        if not performed:
            continue
        try:
            pd = date.fromisoformat(performed[:10])
            if pd < service_date:
                prior.append(p)
        except Exception:
            continue
    return prior


def _slugify(s: str) -> str:
    s = s.lower().strip()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")
