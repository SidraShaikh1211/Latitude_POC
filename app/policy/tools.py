"""Adjudicator tool implementations (Task #17).

Five tools, intentionally narrow — the spec's design intuition is that a
small tool surface produces higher selection accuracy. These are pure
Python; the Anthropic agent loop wires them through `tool_handler`.

Tools:
  - search_facts_by_type(fact_type, filters?) → list of matching facts
  - get_document_excerpt(document_id, page, start?, end?) → text + citation
  - check_temporal_constraint(constraint, dates|durations) → boolean + rationale
  - lookup_term_class(term, target_class, domain?) → in_class + source
  - request_human_review(reason) → structured escalation marker

All tool inputs are JSON-validated against the schemas in `TOOL_DEFINITIONS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app.extraction.intake import ExtractedFacts
from app.extraction.pdf import ExtractedDocument
from app.pas.bundle_parser import FactCollection
from app.policy.term_class import (
    classify_icd10,
    classify_medication,
    lookup_icd10_class,
    lookup_medication_class,
)


# ---------------------------------------------------------------------------
# CaseFacts — aggregate of everything the adjudicator can see
# ---------------------------------------------------------------------------

@dataclass
class CaseFacts:
    """Everything the adjudicator can read while evaluating a criterion.

    - `bundle_facts`: structured resources from the inbound Da Vinci PAS Bundle
    - `extracted`: FHIR-shaped resources from PDF intake (with citations)
    - `documents`: cached ExtractedDocument objects by document_id
    """

    bundle_facts: FactCollection
    extracted: ExtractedFacts | None = None
    documents: dict[str, ExtractedDocument] = field(default_factory=dict)
    escalations: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Tool 1: search_facts_by_type
# ---------------------------------------------------------------------------

VALID_FACT_TYPES = {
    "Condition", "Observation", "MedicationRequest", "MedicationStatement",
    "Procedure", "AllergyIntolerance", "DiagnosticReport",
    "PriorProcedure",  # alias: a Procedure that's flagged as prior (not the requested service)
}


def search_facts_by_type(
    case: CaseFacts,
    fact_type: str,
    filters: dict[str, Any] | None = None,
) -> list[dict]:
    """Return all known facts of the given type, optionally filtered.

    Supported filters:
      - icd10_pattern: glob pattern (e.g., "M54.*") against Condition.icd10_code
      - icd10_class: conceptual grouping name (e.g., "lumbar_radiculopathy")
      - drug_class: medication class name (e.g., "NSAID")
      - since: ISO date — only facts on or after this date
      - until: ISO date — only facts on or before this date
      - body_site_substring: case-insensitive substring match against body_site
      - text_contains: substring match against display / value_string / notes
    """
    filters = filters or {}
    out: list[dict] = []

    if fact_type == "PriorProcedure":
        out.extend(_serialize_procedures(case.bundle_facts.procedures_prior))
    elif fact_type == "Procedure":
        # From intake-extracted (if available)
        if case.extracted:
            out.extend([p.model_dump() for p in case.extracted.procedures])
        out.extend(_serialize_procedures(case.bundle_facts.procedures_prior))
    elif fact_type == "Condition":
        if case.extracted:
            out.extend([c.model_dump() for c in case.extracted.conditions])
        for c in case.bundle_facts.conditions:
            out.append(_thin_bundle_resource(c))
    elif fact_type == "Observation":
        if case.extracted:
            out.extend([o.model_dump() for o in case.extracted.observations])
        for o in case.bundle_facts.observations:
            out.append(_thin_bundle_resource(o))
    elif fact_type in {"MedicationRequest", "MedicationStatement"}:
        if case.extracted:
            for m in case.extracted.medications:
                if m.resource_type == fact_type:
                    out.append(m.model_dump())
        bucket = (
            case.bundle_facts.medication_requests
            if fact_type == "MedicationRequest"
            else case.bundle_facts.medication_statements
        )
        for m in bucket:
            out.append(_thin_bundle_resource(m))
    elif fact_type == "AllergyIntolerance":
        if case.extracted:
            out.extend([a.model_dump() for a in case.extracted.allergies])
        for a in case.bundle_facts.allergies:
            out.append(_thin_bundle_resource(a))
    elif fact_type == "DiagnosticReport":
        if case.extracted:
            out.extend([d.model_dump() for d in case.extracted.diagnostic_reports])
    else:
        return [{
            "error": f"unknown fact_type {fact_type!r}; valid: {sorted(VALID_FACT_TYPES)}",
        }]

    return _apply_filters(out, filters)


def _apply_filters(items: list[dict], filters: dict[str, Any]) -> list[dict]:
    out = items
    if not filters:
        return out

    if "icd10_pattern" in filters:
        import fnmatch
        pat = filters["icd10_pattern"].upper()
        out = [
            it for it in out
            if fnmatch.fnmatchcase((it.get("icd10_code") or _bundle_first_icd(it) or "").upper(), pat)
        ]
    if "icd10_class" in filters:
        cls = filters["icd10_class"]
        out = [
            it for it in out
            if cls in classify_icd10(it.get("icd10_code") or _bundle_first_icd(it) or "")
        ]
    if "drug_class" in filters:
        cls = filters["drug_class"]
        out = [
            it for it in out
            if cls in classify_medication(it.get("medication_name") or _bundle_med_text(it) or "")
        ]
    if "since" in filters:
        since = date.fromisoformat(filters["since"])
        out = [it for it in out if _fact_date(it) and _fact_date(it) >= since]
    if "until" in filters:
        until = date.fromisoformat(filters["until"])
        out = [it for it in out if _fact_date(it) and _fact_date(it) <= until]
    if "body_site_substring" in filters:
        sub = filters["body_site_substring"].lower()
        out = [it for it in out if sub in (it.get("body_site") or "").lower()]
    if "text_contains" in filters:
        sub = filters["text_contains"].lower()
        def hay(it: dict) -> str:
            return " ".join([
                str(it.get("display") or ""),
                str(it.get("value_string") or ""),
                str(it.get("notes") or ""),
                str(it.get("findings") or ""),
            ]).lower()
        out = [it for it in out if sub in hay(it)]
    return out


def _serialize_procedures(procs: list[dict]) -> list[dict]:
    """Thin out an inbound-Bundle Procedure to the fields the adjudicator cares about."""
    out = []
    for p in procs:
        cpt = ""
        display = ""
        for c in (p.get("code") or {}).get("coding") or []:
            cpt = c.get("code", "") or cpt
            display = c.get("display", "") or display
        out.append({
            "resource_type": "Procedure",
            "id": p.get("id"),
            "cpt_code": cpt,
            "display": display,
            "performed_date": (p.get("performedDateTime") or "")[:10] or None,
            "body_site": _first_bundle_body_site(p),
            "source": "bundle",
        })
    return out


def _first_bundle_body_site(p: dict) -> str | None:
    bs_list = p.get("bodySite") or []
    if not bs_list and "bodySite" in p:
        bs_list = [p["bodySite"]]
    for bs in bs_list:
        if isinstance(bs, dict):
            return bs.get("text") or ((bs.get("coding") or [{}])[0].get("display"))
    return None


def _thin_bundle_resource(r: dict) -> dict:
    """Generic thin-out for Condition/Observation/Medication etc. coming from the Bundle."""
    icd = _bundle_first_icd(r)
    med_text = _bundle_med_text(r)
    display = ""
    for path in [["code", "text"], ["code", "coding", 0, "display"]]:
        v = r
        try:
            for k in path:
                v = v[k]  # type: ignore[index]
            if v:
                display = str(v)
                break
        except (KeyError, IndexError, TypeError):
            continue
    return {
        "resource_type": r.get("resourceType"),
        "id": r.get("id"),
        "icd10_code": icd,
        "medication_name": med_text,
        "display": display,
        "value_string": _bundle_value_string(r),
        "value_quantity": _bundle_value_quantity(r),
        "effective_date": _bundle_effective_date(r),
        "source": "bundle",
    }


def _bundle_first_icd(r: dict) -> str | None:
    code = r.get("code") or {}
    for c in code.get("coding") or []:
        sys = (c.get("system") or "").lower()
        if "icd-10" in sys or "icd10" in sys:
            return c.get("code")
    return None


def _bundle_med_text(r: dict) -> str | None:
    mcc = r.get("medicationCodeableConcept") or {}
    text = mcc.get("text")
    if text:
        return text
    for c in mcc.get("coding") or []:
        if c.get("display"):
            return c["display"]
    return None


def _bundle_value_string(r: dict) -> str | None:
    return r.get("valueString")


def _bundle_value_quantity(r: dict) -> float | None:
    vq = r.get("valueQuantity") or {}
    val = vq.get("value")
    return float(val) if val is not None else None


def _bundle_effective_date(r: dict) -> str | None:
    eff = r.get("effectiveDateTime") or r.get("recordedDate") or r.get("authoredOn")
    return eff[:10] if eff else None


def _fact_date(it: dict) -> date | None:
    for k in ("performed_date", "effective_date", "start_date", "onset_date"):
        v = it.get(k)
        if v:
            try:
                return date.fromisoformat(v[:10])
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# Tool 2: get_document_excerpt
# ---------------------------------------------------------------------------


def get_document_excerpt(
    case: CaseFacts,
    document_id: str,
    page: int,
    start_offset: int | None = None,
    end_offset: int | None = None,
    context_chars: int = 200,
) -> dict[str, Any]:
    doc = case.documents.get(document_id)
    if doc is None:
        return {"error": f"document_id {document_id!r} not in case", "available": list(case.documents.keys())}
    if page < 1 or page > doc.page_count:
        return {"error": f"page {page} out of range (1..{doc.page_count})"}
    pt = doc.page(page)
    assert pt is not None
    excerpt = doc.excerpt(page, start_offset, end_offset, context_chars=context_chars)
    return {
        "document_id": document_id,
        "page": page,
        "page_char_count": pt.char_count,
        "excerpt": excerpt,
        "start_offset": start_offset,
        "end_offset": end_offset,
    }


# ---------------------------------------------------------------------------
# Tool 3: check_temporal_constraint
# ---------------------------------------------------------------------------


def check_temporal_constraint(
    constraint: str,
    *,
    duration_weeks: float | None = None,
    duration_days: float | None = None,
    frequency_per_week: float | None = None,
    count: int | None = None,
    dates: list[str] | None = None,
    reference_date: str | None = None,
    threshold_weeks: float | None = None,
    threshold_days: float | None = None,
    threshold_frequency: float | None = None,
    threshold_count: int | None = None,
) -> dict[str, Any]:
    """Evaluate a temporal/quantitative constraint.

    `constraint` selects the check:
      - "min_duration_weeks"      — duration_weeks (or computed from dates) >= threshold_weeks
      - "min_duration_days"       — duration_days >= threshold_days
      - "min_frequency_per_week"  — frequency_per_week >= threshold_frequency
      - "min_count"               — count >= threshold_count
      - "min_days_between"        — min gap among `dates` >= threshold_days
      - "within_last_days"        — `dates[-1]` is within `threshold_days` of reference_date
      - "elapsed_days_since"      — dates[-1] >= reference_date - threshold_days (compatible alias)

    Returns: {"passes": bool, "computed": ..., "threshold": ..., "rationale": str}
    """
    # Helper: compute duration_weeks from dates if provided
    if dates and duration_weeks is None:
        ds = sorted(_parse_date(d) for d in dates if _parse_date(d))
        if len(ds) >= 2:
            delta = (ds[-1] - ds[0]).days
            duration_weeks = delta / 7.0
            duration_days = float(delta)

    if constraint == "min_duration_weeks":
        if duration_weeks is None or threshold_weeks is None:
            return _missing("duration_weeks, threshold_weeks")
        return _result(duration_weeks >= threshold_weeks, duration_weeks, threshold_weeks,
                        f"duration={duration_weeks:.1f}w threshold={threshold_weeks:.1f}w")

    if constraint == "min_duration_days":
        if duration_days is None or threshold_days is None:
            return _missing("duration_days, threshold_days")
        return _result(duration_days >= threshold_days, duration_days, threshold_days,
                        f"duration={duration_days:.0f}d threshold={threshold_days:.0f}d")

    if constraint == "min_frequency_per_week":
        if frequency_per_week is None or threshold_frequency is None:
            return _missing("frequency_per_week, threshold_frequency")
        return _result(frequency_per_week >= threshold_frequency, frequency_per_week, threshold_frequency,
                        f"freq={frequency_per_week:.1f}/wk threshold={threshold_frequency:.1f}/wk")

    if constraint == "min_count":
        if count is None or threshold_count is None:
            return _missing("count, threshold_count")
        return _result(count >= threshold_count, count, threshold_count,
                        f"count={count} threshold={threshold_count}")

    if constraint == "min_days_between":
        if not dates or threshold_days is None:
            return _missing("dates (>=2), threshold_days")
        ds = sorted(d for d in (_parse_date(x) for x in dates) if d)
        if len(ds) < 2:
            return _missing("dates with >=2 valid entries")
        gaps = [(b - a).days for a, b in zip(ds, ds[1:])]
        min_gap = min(gaps)
        return _result(min_gap >= threshold_days, min_gap, threshold_days,
                        f"min_gap={min_gap}d threshold={threshold_days:.0f}d")

    if constraint in ("within_last_days", "elapsed_days_since"):
        if not dates or threshold_days is None or reference_date is None:
            return _missing("dates, threshold_days, reference_date")
        ref = _parse_date(reference_date)
        last = sorted(d for d in (_parse_date(x) for x in dates) if d)[-1] if dates else None
        if not ref or not last:
            return _missing("valid dates and reference_date")
        elapsed = (ref - last).days
        return _result(0 <= elapsed <= threshold_days, elapsed, threshold_days,
                        f"elapsed={elapsed}d threshold={threshold_days:.0f}d")

    return {"error": f"unknown constraint {constraint!r}"}


def _result(ok: bool, computed: Any, threshold: Any, rationale: str) -> dict:
    return {"passes": bool(ok), "computed": computed, "threshold": threshold, "rationale": rationale}


def _missing(args: str) -> dict:
    return {"passes": False, "rationale": f"missing required arg(s): {args}", "computed": None, "threshold": None}


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Tool 4: lookup_term_class
# ---------------------------------------------------------------------------


def lookup_term_class_tool(
    term: str, target_class: str, domain: str = "medication"
) -> dict[str, Any]:
    if domain == "medication":
        r = lookup_medication_class(term, target_class)
    elif domain == "icd10":
        r = lookup_icd10_class(term, target_class)
    else:
        return {"error": f"unknown domain {domain!r}; use 'medication' or 'icd10'"}
    return {
        "term": r.term,
        "target_class": target_class,
        "in_class": r.in_class,
        "source": r.source,
        "explanation": r.explanation,
    }


# ---------------------------------------------------------------------------
# Tool 5: request_human_review
# ---------------------------------------------------------------------------


def request_human_review(case: CaseFacts, reason: str, criterion_id: str | None = None) -> dict[str, Any]:
    label = f"[{criterion_id}] {reason}" if criterion_id else reason
    case.escalations.append(label)
    return {"escalated": True, "reason": label}


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool-use JSON schemas)
# ---------------------------------------------------------------------------


def tool_definitions() -> list[dict]:
    return [
        {
            "name": "search_facts_by_type",
            "description": (
                "Return clinical facts of a given FHIR resource type from the case "
                "(inbound Bundle + intake-extracted). Optionally filter by ICD-10 "
                "pattern/class, medication class, date window, body site, or text substring."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "fact_type": {
                        "type": "string",
                        "enum": sorted(VALID_FACT_TYPES),
                    },
                    "filters": {
                        "type": "object",
                        "additionalProperties": True,
                        "description": (
                            "Optional filters: icd10_pattern, icd10_class, drug_class, "
                            "since, until, body_site_substring, text_contains."
                        ),
                    },
                },
                "required": ["fact_type"],
            },
        },
        {
            "name": "get_document_excerpt",
            "description": (
                "Fetch text from a clinical document for verbatim quoting. Returns "
                "the page text with optional offset-window expansion for context."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "document_id": {"type": "string"},
                    "page": {"type": "integer", "minimum": 1},
                    "start_offset": {"type": "integer", "minimum": 0},
                    "end_offset": {"type": "integer", "minimum": 0},
                    "context_chars": {"type": "integer", "minimum": 0, "default": 200},
                },
                "required": ["document_id", "page"],
            },
        },
        {
            "name": "check_temporal_constraint",
            "description": (
                "Evaluate temporal / numeric constraints (PT duration >=4 weeks, "
                "frequency >=3 sessions/week, time since last event, etc.). "
                "Returns passes + computed value + threshold."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "constraint": {
                        "type": "string",
                        "enum": [
                            "min_duration_weeks", "min_duration_days",
                            "min_frequency_per_week", "min_count",
                            "min_days_between", "within_last_days", "elapsed_days_since",
                        ],
                    },
                    "duration_weeks": {"type": "number"},
                    "duration_days": {"type": "number"},
                    "frequency_per_week": {"type": "number"},
                    "count": {"type": "integer"},
                    "dates": {"type": "array", "items": {"type": "string"}},
                    "reference_date": {"type": "string"},
                    "threshold_weeks": {"type": "number"},
                    "threshold_days": {"type": "number"},
                    "threshold_frequency": {"type": "number"},
                    "threshold_count": {"type": "integer"},
                },
                "required": ["constraint"],
            },
        },
        {
            "name": "lookup_term_class",
            "description": (
                "Resolve whether a medication or ICD-10 code belongs to a target "
                "class (e.g., 'ibuprofen' in 'NSAID', 'M54.16' in 'lumbar_radiculopathy'). "
                "Dictionary first; LLM fallback for unknowns. Returns in_class + source."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "target_class": {"type": "string"},
                    "domain": {"type": "string", "enum": ["medication", "icd10"], "default": "medication"},
                },
                "required": ["term", "target_class"],
            },
        },
        {
            "name": "request_human_review",
            "description": (
                "Flag the current criterion for human review. Use when the criterion "
                "is structurally ambiguous or the evidence is contradictory beyond what "
                "the four-valued verdict can express. This is a structured escalation, "
                "not a verdict override."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "minLength": 5},
                    "criterion_id": {"type": "string"},
                },
                "required": ["reason"],
            },
        },
    ]


# ---------------------------------------------------------------------------
# Single dispatch entry point used by the agent loop
# ---------------------------------------------------------------------------


def dispatch_tool(case: CaseFacts, tool_name: str, tool_input: dict[str, Any]) -> Any:
    if tool_name == "search_facts_by_type":
        return search_facts_by_type(
            case,
            tool_input["fact_type"],
            tool_input.get("filters"),
        )
    if tool_name == "get_document_excerpt":
        return get_document_excerpt(
            case,
            tool_input["document_id"],
            int(tool_input["page"]),
            tool_input.get("start_offset"),
            tool_input.get("end_offset"),
            int(tool_input.get("context_chars", 200)),
        )
    if tool_name == "check_temporal_constraint":
        return check_temporal_constraint(
            tool_input["constraint"],
            duration_weeks=tool_input.get("duration_weeks"),
            duration_days=tool_input.get("duration_days"),
            frequency_per_week=tool_input.get("frequency_per_week"),
            count=tool_input.get("count"),
            dates=tool_input.get("dates"),
            reference_date=tool_input.get("reference_date"),
            threshold_weeks=tool_input.get("threshold_weeks"),
            threshold_days=tool_input.get("threshold_days"),
            threshold_frequency=tool_input.get("threshold_frequency"),
            threshold_count=tool_input.get("threshold_count"),
        )
    if tool_name == "lookup_term_class":
        return lookup_term_class_tool(
            tool_input["term"],
            tool_input["target_class"],
            tool_input.get("domain", "medication"),
        )
    if tool_name == "request_human_review":
        return request_human_review(
            case,
            tool_input["reason"],
            tool_input.get("criterion_id"),
        )
    return {"error": f"unknown tool {tool_name!r}"}
