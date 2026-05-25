"""Deterministic reconciliation: metadata routing codes vs intake-extracted facts.

The metadata extractor (PAS Bundle's Claim.diagnosis + Claim.item.productOrService)
and the intake extractor (Claude over the PDF) are two independent views of the
same case. When they disagree, the selector routes by metadata while the
adjudicator sees only intake facts — producing confusing `not_documented`
verdicts for evidence that was, in effect, submitted under a different code.

This module is the deterministic safety harness. Pure Python, no LLM:

  - Each metadata indication (the ICD-10s linked to the requested item via
    diagnosisSequence) is checked against intake's Condition list.
  - Exact match → no warning. Same ICD-10 family (3-char prefix) → warn about
    possible miscoding. No match in either → warn that intake has no
    supporting Condition.

Warnings flow to the reviewer as advisory context; they do NOT change
selector routing or block adjudication. The principle: LLMs only where
judgment is required, deterministic everywhere else.
"""

from __future__ import annotations

from app.extraction.intake import ExtractedFacts
from app.pas.bundle_parser import CaseContext


def reconcile_metadata_vs_intake(
    context: CaseContext,
    extracted: ExtractedFacts,
) -> list[str]:
    """Return human-readable warnings; empty list when metadata and intake agree.

    Compares `context.requested_indication_icd10_codes` (the indications the
    requested CPT is being claimed against — conceptually the primary
    indications) with the ICD-10 codes the intake extractor pulled from the
    PDF into `extracted.conditions[].icd10_code`.
    """
    intake_codes = _intake_codes(extracted)
    intake_families = {_family(c) for c in intake_codes if _family(c)}
    displays = context.indication_displays

    warnings: list[str] = []
    seen: set[str] = set()
    for raw in context.requested_indication_icd10_codes:
        code = _normalize(raw)
        if not code or code in seen:
            continue
        seen.add(code)

        if code in intake_codes:
            continue

        fam = _family(code)
        related = sorted(c for c in intake_codes if _family(c) == fam) if fam else []
        meta_label = _label(code, displays)
        if related:
            related_label = ", ".join(_label(c, displays) for c in related)
            warnings.append(
                f"Metadata indication {meta_label} not found in extracted "
                f"Conditions, but related codes from the same family ({fam}) "
                f"were extracted: {related_label}. Possible miscoding."
            )
        else:
            warnings.append(
                f"Metadata indication {meta_label} has no supporting Condition "
                f"in the extracted chart. Routing may be based on a code the "
                f"clinical narrative does not document."
            )

    return warnings


def _intake_codes(extracted: ExtractedFacts) -> set[str]:
    return {
        _normalize(c.icd10_code)
        for c in extracted.conditions
        if c.icd10_code and _normalize(c.icd10_code)
    }


def _normalize(code: str | None) -> str:
    if not code:
        return ""
    return code.strip().upper()


def _family(code: str) -> str:
    code = _normalize(code)
    if not code:
        return ""
    return code.split(".", 1)[0]


def _label(code: str, displays: dict[str, str]) -> str:
    disp = (displays.get(code) or displays.get(code.lower()) or "").strip()
    return f"{code} ({disp})" if disp else code
