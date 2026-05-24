"""Deterministic short-circuit for structurally-checkable criteria.

Many policy leaves are pure pattern checks — `icd10_pattern: M54.*`,
`age_min: 18`, "is medication X in the NSAID class?". For these, there is
no judgment call: the data either matches or it doesn't. Running an LLM
agent loop just to compute `M54.16 matches M54.*` wastes both tokens and
wall-clock.

This module attempts a deterministic verdict from CaseFacts alone. If it
can answer confidently it returns a `CriterionVerdict`; if it cannot, it
returns `None` and the caller falls back to the LLM adjudicator. The
contract is intentionally conservative — we only short-circuit when the
answer is mechanical, never when it requires interpretation.

Handlers (initial set):
  - `diagnosis_code`        — ICD-10 glob match against Condition.icd10_code
  - `numeric_threshold`     — only `age_min` / `age_max` against Patient.birth_date
  - `class_membership`      — medication class via term_class dictionary
  - `numeric_count`         — only `min_total_sessions` (PT sessions via therapy CPTs)

Add more handlers here as the policy library grows. Anything not handled
falls through to the LLM, so adding kinds is purely additive.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from app.policy.registry import CriterionLeaf, Exclusion
from app.policy.term_class import is_therapy_cpt, lookup_medication_class
from app.policy.tools import CaseFacts
from app.policy.verdict import CriterionVerdict, PatientEvidence


log = structlog.get_logger()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def try_deterministic_verdict(
    node: CriterionLeaf | Exclusion,
    case: CaseFacts,
) -> CriterionVerdict | None:
    """Return a verdict if the criterion is structurally resolvable; else None.

    Returning None is *not* a failure — it just signals that the caller should
    run the LLM adjudicator. Handlers MUST return None whenever they encounter
    ambiguity or unknown data (e.g., medication not in dictionary). The LLM is
    the safety net; we only steal work from it that is unambiguous.
    """
    ev = node.evaluation or {}
    kind = ev.get("evaluator_kind")
    handler = _HANDLERS.get(kind or "")
    if handler is None:
        return None
    try:
        verdict = handler(node, case, ev)
    except Exception as e:
        log.warning(
            "deterministic_eval.handler_error",
            criterion_id=node.id,
            kind=kind,
            error=str(e),
        )
        return None
    if verdict is not None:
        log.info(
            "deterministic_eval.resolved",
            criterion_id=node.id,
            kind=kind,
            verdict=verdict.verdict,
        )
    return verdict


# ---------------------------------------------------------------------------
# Handler: diagnosis_code
# ---------------------------------------------------------------------------


def _eval_diagnosis_code(
    node: CriterionLeaf | Exclusion, case: CaseFacts, ev: dict[str, Any]
) -> CriterionVerdict | None:
    """Glob-match patient ICD-10 codes against the policy's pattern list.

    Two value_constraint shapes appear in the policies today:
      - `icd10_qualifying_patterns`: ["M54.1*", "M50.1*"]  → met if any patient
        Condition.icd10_code matches; not_documented otherwise.
      - `non_qualifying_primary_icd10` / `non_qualifying_icd10_patterns`: the
        criterion (an exclusion) fires (`met`) if any patient code matches;
        else `not_met`.
    """
    vc = ev.get("value_constraints") or {}
    qualifying = vc.get("icd10_qualifying_patterns")
    non_qualifying = vc.get("non_qualifying_icd10_patterns") or vc.get("non_qualifying_primary_icd10")

    if not qualifying and not non_qualifying:
        return None

    codes = _patient_icd10_codes(case)
    if not codes:
        return _verdict(
            node.id,
            "not_documented",
            confidence=0.95,
            reasoning="No ICD-10 coded diagnoses found in case to evaluate against the criterion's pattern list.",
            missing_info=["At least one Condition with an ICD-10 code"],
        )

    if qualifying:
        matches = _matching_codes(codes, qualifying)
        if matches:
            return _verdict(
                node.id, "met", confidence=0.98,
                reasoning=(
                    f"Patient has qualifying diagnosis code(s) {sorted(matches)} "
                    f"matching pattern(s) {qualifying}."
                ),
                patient_evidence=[
                    PatientEvidence(
                        fhir_resource_id=None,
                        fact_type="Condition",
                        value_summary=f"ICD-10 {c} matches qualifying pattern",
                        verified=True,
                    )
                    for c in sorted(matches)
                ],
            )
        return _verdict(
            node.id, "not_documented", confidence=0.9,
            reasoning=(
                f"No diagnosis codes in {sorted(codes)} match qualifying patterns {qualifying}."
            ),
            missing_info=[f"A Condition with ICD-10 matching one of: {qualifying}"],
        )

    # non_qualifying flavor: an exclusion that fires when the code IS present
    matches = _matching_codes(codes, non_qualifying)
    if matches:
        return _verdict(
            node.id, "met", confidence=0.98,
            reasoning=(
                f"Excluded diagnosis code(s) {sorted(matches)} present, matching "
                f"non-qualifying pattern(s) {non_qualifying}."
            ),
            patient_evidence=[
                PatientEvidence(
                    fact_type="Condition",
                    value_summary=f"ICD-10 {c} is on the exclusion list",
                    verified=True,
                )
                for c in sorted(matches)
            ],
        )
    return _verdict(
        node.id, "not_met", confidence=0.95,
        reasoning=(
            f"No diagnosis codes in {sorted(codes)} match the excluded patterns {non_qualifying}; "
            "exclusion does not fire."
        ),
    )


# ---------------------------------------------------------------------------
# Handler: numeric_threshold (only age-based variants for now)
# ---------------------------------------------------------------------------


def _eval_numeric_threshold(
    node: CriterionLeaf | Exclusion, case: CaseFacts, ev: dict[str, Any]
) -> CriterionVerdict | None:
    """Age-based numeric_threshold only. Other shapes (NRS thresholds, %
    pain relief, ...) require fuzzy matching of *which* observation is the
    pain score, which the LLM does better — defer for those."""
    vc = ev.get("value_constraints") or {}
    age_min = vc.get("age_min")
    age_max = vc.get("age_max")
    if age_min is None and age_max is None:
        return None  # not an age check — defer

    age = _patient_age(case)
    if age is None:
        return _verdict(
            node.id, "not_documented", confidence=0.9,
            reasoning="Patient birth date and/or service date unavailable; cannot compute age.",
            missing_info=["Patient.birthDate", "Claim service date"],
        )

    fails_min = age_min is not None and age < age_min
    fails_max = age_max is not None and age > age_max
    if fails_min or fails_max:
        bound = (
            f">= {age_min}" if fails_min else f"<= {age_max}"
        )
        return _verdict(
            node.id, "not_met", confidence=1.0,
            reasoning=f"Patient age {age} does not satisfy required bound (age must be {bound}).",
            patient_evidence=[PatientEvidence(
                fact_type="Patient",
                value_summary=f"age={age}",
                verified=True,
            )],
        )
    return _verdict(
        node.id, "met", confidence=1.0,
        reasoning=(
            f"Patient age {age} satisfies the age constraint"
            f"{f' (>= {age_min})' if age_min is not None else ''}"
            f"{f' (<= {age_max})' if age_max is not None else ''}."
        ),
        patient_evidence=[PatientEvidence(
            fact_type="Patient",
            value_summary=f"age={age}",
            verified=True,
        )],
    )


# ---------------------------------------------------------------------------
# Handler: class_membership (dictionary-only — defer if any term unknown)
# ---------------------------------------------------------------------------


def _eval_class_membership(
    node: CriterionLeaf | Exclusion, case: CaseFacts, ev: dict[str, Any]
) -> CriterionVerdict | None:
    """Determine whether the patient has *any* medication in `qualifying_classes`.

    We only short-circuit when EVERY candidate medication resolves via the
    hand-authored dictionary (`source == "dictionary"`). If any med is unknown
    we defer — the LLM with `lookup_term_class` (which has an LLM fallback)
    can handle it.
    """
    vc = ev.get("value_constraints") or {}
    qualifying_classes = vc.get("qualifying_classes")
    if not qualifying_classes:
        return None

    meds = _patient_medication_names(case)
    if not meds:
        return _verdict(
            node.id, "not_documented", confidence=0.9,
            reasoning=(
                "No medications recorded in case to evaluate against qualifying "
                f"classes {qualifying_classes}."
            ),
            missing_info=["MedicationRequest or MedicationStatement entries"],
        )

    matched: list[tuple[str, str]] = []
    unknown: list[str] = []
    for name in meds:
        # An unknown med might still belong to a target class — only the
        # dictionary's positive answer or *full coverage* of negatives is
        # definitive. Check every class before deciding the med is unknown.
        any_dict_hit = False
        any_unknown = False
        for cls in qualifying_classes:
            r = lookup_medication_class(name, cls)
            if r.source == "dictionary" and r.in_class:
                matched.append((name, cls))
                any_dict_hit = True
                break
            if r.source == "unknown":
                any_unknown = True
        if any_dict_hit:
            continue
        if any_unknown:
            unknown.append(name)
    if matched:
        return _verdict(
            node.id, "met", confidence=0.95,
            reasoning=(
                "Patient has medication(s) in qualifying classes: "
                + ", ".join(f"{n}→{c}" for n, c in matched)
            ),
            patient_evidence=[
                PatientEvidence(
                    fact_type="MedicationStatement",
                    value_summary=f"{name} (class: {cls})",
                    verified=True,
                )
                for name, cls in matched
            ],
        )

    if unknown:
        # Unknown med could belong to a qualifying class; let the LLM decide.
        return None

    return _verdict(
        node.id, "not_met", confidence=0.9,
        reasoning=(
            f"None of the patient medications {sorted(set(meds))} dictionary-match "
            f"any of the qualifying classes {qualifying_classes}."
        ),
    )


# ---------------------------------------------------------------------------
# Handler: numeric_count (only min_total_sessions for PT)
# ---------------------------------------------------------------------------


def _eval_numeric_count(
    node: CriterionLeaf | Exclusion, case: CaseFacts, ev: dict[str, Any]
) -> CriterionVerdict | None:
    """Count completed PT sessions and compare to `min_total_sessions`.

    A "session" = a distinct date on which the patient had at least one
    procedure with a therapy CPT (97110/97112/97140/97530/97161-4). Counting
    Procedure resources directly would over-count when one visit logs
    multiple CPTs, so we group by `performed_date`.

    Asymmetric rule: we can return `met` deterministically when the structured
    count alone clears the threshold, but a count below threshold defers to
    the LLM — narrative notes may document additional visits the structured
    Procedure resources missed. This keeps the handler from producing false
    `not_met` verdicts when the truth is "documentation is sparse."

    Other numeric_count shapes (`max_count_per_region`, `max_count_per_region_rolling_12mo`)
    require body-region grouping and are left to the LLM for now.
    """
    vc = ev.get("value_constraints") or {}
    threshold = vc.get("min_total_sessions")
    if threshold is None:
        return None  # other numeric_count shapes — defer

    session_dates = _therapy_session_dates(case)
    count = len(session_dates)
    if count < threshold:
        return None  # might be more in notes; let the LLM look

    return _verdict(
        node.id, "met", confidence=0.95,
        reasoning=(
            f"Patient has {count} distinct therapy-session date(s) "
            f"({sorted(session_dates)[:5]}{'...' if len(session_dates) > 5 else ''}), "
            f"meeting the {threshold}-session threshold."
        ),
        patient_evidence=[PatientEvidence(
            fact_type="Procedure",
            value_summary=f"{count} therapy sessions across distinct dates >= {threshold}",
            verified=True,
        )],
    )


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


_HANDLERS = {
    "diagnosis_code": _eval_diagnosis_code,
    "numeric_threshold": _eval_numeric_threshold,
    "class_membership": _eval_class_membership,
    "numeric_count": _eval_numeric_count,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _verdict(
    criterion_id: str,
    verdict: str,
    *,
    confidence: float,
    reasoning: str,
    patient_evidence: list[PatientEvidence] | None = None,
    missing_info: list[str] | None = None,
) -> CriterionVerdict:
    return CriterionVerdict(
        criterion_id=criterion_id,
        verdict=verdict,  # type: ignore[arg-type]
        confidence=confidence,
        patient_evidence=patient_evidence or [],
        reasoning=reasoning,
        missing_info=missing_info or [],
    )


def _patient_icd10_codes(case: CaseFacts) -> set[str]:
    """All ICD-10 codes attached to the patient, from intake + bundle."""
    out: set[str] = set()
    if case.extracted is not None:
        for c in case.extracted.conditions:
            if c.icd10_code:
                out.add(c.icd10_code.upper())
    for c in case.bundle_facts.conditions:
        code = (c.get("code") or {})
        for coding in code.get("coding") or []:
            sys = (coding.get("system") or "").lower()
            if "icd-10" in sys or "icd10" in sys:
                v = coding.get("code")
                if v:
                    out.add(v.upper())
    return out


def _matching_codes(codes: set[str], patterns: list[str]) -> set[str]:
    matched: set[str] = set()
    for code in codes:
        for pat in patterns:
            if fnmatch.fnmatchcase(code, pat.upper()):
                matched.add(code)
                break
    return matched


def _patient_age(case: CaseFacts) -> int | None:
    """Patient age at service date. Returns None if either side is missing."""
    birth = _patient_birth_date(case)
    if birth is None or case.service_date is None:
        return None
    age = case.service_date.year - birth.year
    if (case.service_date.month, case.service_date.day) < (birth.month, birth.day):
        age -= 1
    return age


def _patient_birth_date(case: CaseFacts) -> date | None:
    if case.extracted and case.extracted.patient and case.extracted.patient.birth_date:
        try:
            return date.fromisoformat(case.extracted.patient.birth_date[:10])
        except ValueError:
            pass
    if case.bundle_facts.patient:
        bd = case.bundle_facts.patient.get("birthDate")
        if bd:
            try:
                return date.fromisoformat(bd[:10])
            except ValueError:
                pass
    return None


def _therapy_session_dates(case: CaseFacts) -> set[str]:
    """Distinct dates (YYYY-MM-DD) on which the patient had at least one PT
    CPT. Pulls from both intake-extracted Procedures and bundle prior
    Procedures."""
    dates: set[str] = set()
    if case.extracted is not None:
        for p in case.extracted.procedures:
            if p.cpt_code and is_therapy_cpt(p.cpt_code) and p.performed_date:
                dates.add(p.performed_date[:10])
    for p in case.bundle_facts.procedures_prior:
        cpt = ""
        for c in (p.get("code") or {}).get("coding") or []:
            cpt = c.get("code", "") or cpt
        performed = p.get("performedDateTime") or ""
        if cpt and is_therapy_cpt(cpt) and performed:
            dates.add(performed[:10])
    return dates


def _patient_medication_names(case: CaseFacts) -> list[str]:
    """Every medication name we've seen in the case (intake + bundle), order
    preserved, duplicates kept so per-med dictionary lookups can record each
    one individually."""
    names: list[str] = []
    if case.extracted is not None:
        for m in case.extracted.medications:
            if m.medication_name:
                names.append(m.medication_name)
    for r in case.bundle_facts.medication_requests + case.bundle_facts.medication_statements:
        mcc = r.get("medicationCodeableConcept") or {}
        text = mcc.get("text")
        if text:
            names.append(text)
            continue
        for c in mcc.get("coding") or []:
            if c.get("display"):
                names.append(c["display"])
                break
    return names
