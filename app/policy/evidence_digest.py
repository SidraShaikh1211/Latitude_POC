"""Per-case evidence digest — a compact, deterministic summary of CaseFacts
that is built ONCE per case and reused across every leaf adjudication.

Motivation: without a digest, each per-leaf agent starts cold and must spend
2-3 tool iterations probing the case structure (`search_facts_by_type` for
Conditions, Observations, MedicationRequests, …) before it can reason. With
22 leaves that compounds into hundreds of redundant tool round-trips.

The digest is plain text — purely deterministic — and is placed in the
*system* slot with `cache_control` so all parallel leaves read it from the
prompt cache (~10% the cost of fresh input). Each leaf still has the full
adjudicator toolset to drill deeper when the digest is insufficient.

Intentionally *summary only*: we list facts and their identifiers, but the
verbatim quotes still need to come from the documents via `get_document_excerpt`.
That keeps the digest small (~1-3k tokens) and forces verifiable citations.
"""

from __future__ import annotations

from app.extraction.intake import ExtractedFacts
from app.pas.bundle_parser import CaseContext, FactCollection
from app.policy.tools import (
    CaseFacts,
    _bundle_effective_date,
    _bundle_first_icd,
    _bundle_med_text,
    _bundle_value_quantity,
    _first_bundle_body_site,
)


MAX_ITEMS_PER_SECTION = 40
MAX_LINE_CHARS = 220


def build_evidence_digest(
    case: CaseFacts,
    context: CaseContext | None = None,
    branch: str | None = None,
) -> str:
    """Render a compact bulleted digest of everything the adjudicator can see.

    Order: requested service (when context given) → documents → patient →
    conditions → observations → medications → procedures → allergies →
    diagnostic reports. Each section is capped so a pathological case can't
    blow out the prompt.

    `context` and `branch` are optional so single-leaf test paths
    (`adjudicate_criterion` building its own digest) keep working.
    """
    sections: list[str] = []

    # --- Requested service (only when CaseContext is wired in)
    if context is not None:
        sections.append(_requested_service_block(context, branch))

    # --- Documents
    if case.documents:
        lines = []
        for doc_id, doc in case.documents.items():
            lines.append(f"- {doc_id} ({doc.page_count} pages)")
        sections.append("DOCUMENTS:\n" + "\n".join(lines))

    bf = case.bundle_facts
    ex = case.extracted

    # --- Patient (intake takes precedence if present)
    patient_line = _patient_line(bf, ex)
    if patient_line:
        sections.append(f"PATIENT:\n- {patient_line}")

    # --- Conditions
    cond_lines = _condition_lines(bf, ex)
    if cond_lines:
        sections.append("CONDITIONS:\n" + "\n".join(cond_lines))

    # --- Observations
    obs_lines = _observation_lines(bf, ex)
    if obs_lines:
        sections.append("OBSERVATIONS:\n" + "\n".join(obs_lines))

    # --- Medications
    med_lines = _medication_lines(bf, ex)
    if med_lines:
        sections.append("MEDICATIONS:\n" + "\n".join(med_lines))

    # --- Procedures (prior + intake-extracted)
    proc_lines = _procedure_lines(bf, ex)
    if proc_lines:
        sections.append("PROCEDURES (prior / completed):\n" + "\n".join(proc_lines))

    # --- Allergies
    allergy_lines = _allergy_lines(bf, ex)
    if allergy_lines:
        sections.append("ALLERGIES:\n" + "\n".join(allergy_lines))

    # --- Diagnostic reports (intake only — bundle puts these on Observation)
    diag_lines = _diag_report_lines(ex)
    if diag_lines:
        sections.append("DIAGNOSTIC REPORTS:\n" + "\n".join(diag_lines))

    if not sections:
        return "CASE EVIDENCE DIGEST: (no facts available — call adjudicator tools to probe)"

    header = (
        "CASE EVIDENCE DIGEST (deterministic summary of CaseFacts; identifiers "
        "shown so you can drill in via tools. Quotes must still be fetched and "
        "verified via get_document_excerpt before citing.)\n"
    )
    return header + "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Section renderers — each returns a list of bullet lines.
# ---------------------------------------------------------------------------


def _requested_service_block(ctx: CaseContext, branch: str | None) -> str:
    """Render the REQUESTED SERVICE bullet block.

    This is the block leaf adjudicators use to (a) tell the doctor's *request*
    apart from prior clinical events in the digest, (b) do time math against
    the service date without a tool call, (c) cross-check the body site against
    Procedure/Observation body_site fields, and (d) reject circular citations
    that quote the request itself as evidence of the indication (see SKILL.md
    "Using the REQUESTED SERVICE block").
    """
    indications = ctx.requested_indication_icd10_codes
    primary_code = indications[0] if indications else ""
    secondary_codes = indications[1:]
    cpt_line = f"- CPT: {ctx.cpt_code}"
    if ctx.cpt_display:
        cpt_line += f" — {ctx.cpt_display}"
    lines = [cpt_line]
    if primary_code:
        lines.append(f"- Primary indication: {_fmt_indication(primary_code, ctx.indication_displays)}")
    if secondary_codes:
        rendered = "; ".join(
            _fmt_indication(c, ctx.indication_displays) for c in secondary_codes
        )
        lines.append(f"- Secondary indications: {rendered}")
    lines.append(f"- Service date: {ctx.service_date.isoformat()}")
    if ctx.body_site:
        lines.append(f"- Body site: {ctx.body_site}")
    lines.append(f"- Urgency: {ctx.urgency}")
    lines.append(f"- Care setting: {ctx.care_setting}")
    if branch:
        lines.append(f"- Branch: {branch}")
    return "REQUESTED SERVICE:\n" + "\n".join(lines)


def _fmt_indication(code: str, displays: dict[str, str]) -> str:
    disp = displays.get(code, "").strip()
    return f"{code} — {disp}" if disp else code


def _patient_line(bf: FactCollection, ex: ExtractedFacts | None) -> str:
    if ex is not None and ex.patient is not None:
        p = ex.patient
        parts = []
        if p.given or p.family:
            parts.append(f"name={p.given or ''} {p.family or ''}".strip())
        if p.birth_date:
            parts.append(f"dob={p.birth_date}")
        if p.gender:
            parts.append(f"gender={p.gender}")
        if p.mrn:
            parts.append(f"mrn={p.mrn}")
        return ", ".join(parts) or "patient present (no demographics extracted)"
    if bf.patient:
        b = bf.patient.get("birthDate")
        g = bf.patient.get("gender")
        return f"dob={b} gender={g}" if b or g else "patient present in bundle"
    return ""


def _condition_lines(bf: FactCollection, ex: ExtractedFacts | None) -> list[str]:
    out: list[str] = []
    if ex is not None:
        for c in ex.conditions[:MAX_ITEMS_PER_SECTION]:
            cite = _first_citation(c)
            out.append(_truncate(
                f"- [{c.icd10_code or '—'}] {c.display}"
                f"{' onset=' + c.onset_date if c.onset_date else ''}"
                f"{' status=' + c.clinical_status if c.clinical_status else ''}"
                f"{cite}"
            ))
    remaining = MAX_ITEMS_PER_SECTION - len(out)
    for c in bf.conditions[:max(0, remaining)]:
        icd = _bundle_first_icd(c) or "—"
        text = (c.get("code") or {}).get("text") or ""
        date = _bundle_effective_date(c)
        out.append(_truncate(
            f"- [{icd}] {text}{' onset=' + date if date else ''}  (bundle:{c.get('id')})"
        ))
    return out


def _observation_lines(bf: FactCollection, ex: ExtractedFacts | None) -> list[str]:
    out: list[str] = []
    if ex is not None:
        for o in ex.observations[:MAX_ITEMS_PER_SECTION]:
            v = _obs_value(o.value_quantity, o.value_unit, o.value_string)
            cite = _first_citation(o)
            out.append(_truncate(
                f"- {o.code_display}={v}"
                f"{' on ' + o.effective_date if o.effective_date else ''}"
                f"{' [' + o.category + ']' if o.category else ''}"
                f"{' body=' + o.body_site if o.body_site else ''}"
                f"{cite}"
            ))
    remaining = MAX_ITEMS_PER_SECTION - len(out)
    for o in bf.observations[:max(0, remaining)]:
        display = ((o.get("code") or {}).get("text")
                   or ((o.get("code") or {}).get("coding") or [{}])[0].get("display", ""))
        vq = _bundle_value_quantity(o)
        date = _bundle_effective_date(o)
        val = f"={vq}" if vq is not None else ""
        out.append(_truncate(
            f"- {display}{val}{' on ' + date if date else ''}  (bundle:{o.get('id')})"
        ))
    return out


def _medication_lines(bf: FactCollection, ex: ExtractedFacts | None) -> list[str]:
    out: list[str] = []
    if ex is not None:
        for m in ex.medications[:MAX_ITEMS_PER_SECTION]:
            cite = _first_citation(m)
            dates = ""
            if m.start_date or m.stop_date:
                dates = f" ({m.start_date or '?'}→{m.stop_date or '?'})"
            out.append(_truncate(
                f"- [{m.resource_type}] {m.medication_name}"
                f"{' ' + m.dose if m.dose else ''}"
                f"{' ' + m.frequency if m.frequency else ''}"
                f"{dates}{cite}"
            ))
    remaining = MAX_ITEMS_PER_SECTION - len(out)
    bundle_meds = (
        [("MedicationRequest", r) for r in bf.medication_requests]
        + [("MedicationStatement", r) for r in bf.medication_statements]
    )
    for kind, m in bundle_meds[:max(0, remaining)]:
        name = _bundle_med_text(m) or "(unnamed)"
        date = _bundle_effective_date(m)
        out.append(_truncate(
            f"- [{kind}] {name}{' on ' + date if date else ''}  (bundle:{m.get('id')})"
        ))
    return out


def _procedure_lines(bf: FactCollection, ex: ExtractedFacts | None) -> list[str]:
    out: list[str] = []
    if ex is not None:
        for p in ex.procedures[:MAX_ITEMS_PER_SECTION]:
            cite = _first_citation(p)
            out.append(_truncate(
                f"- [{p.cpt_code or '—'}] {p.display}"
                f"{' on ' + p.performed_date if p.performed_date else ''}"
                f"{' body=' + p.body_site if p.body_site else ''}"
                f"{cite}"
            ))
    remaining = MAX_ITEMS_PER_SECTION - len(out)
    for p in bf.procedures_prior[:max(0, remaining)]:
        cpt = ""
        display = ""
        for c in (p.get("code") or {}).get("coding") or []:
            cpt = c.get("code", "") or cpt
            display = c.get("display", "") or display
        date = (p.get("performedDateTime") or "")[:10]
        body = _first_bundle_body_site(p)
        out.append(_truncate(
            f"- [{cpt or '—'}] {display}"
            f"{' on ' + date if date else ''}"
            f"{' body=' + body if body else ''}"
            f"  (bundle:{p.get('id')})"
        ))
    return out


def _allergy_lines(bf: FactCollection, ex: ExtractedFacts | None) -> list[str]:
    out: list[str] = []
    if ex is not None:
        for a in ex.allergies[:MAX_ITEMS_PER_SECTION]:
            cite = _first_citation(a)
            out.append(_truncate(
                f"- {a.substance}"
                f"{' reaction=' + a.reaction if a.reaction else ''}"
                f"{' criticality=' + a.criticality if a.criticality else ''}"
                f"{cite}"
            ))
    remaining = MAX_ITEMS_PER_SECTION - len(out)
    for a in bf.allergies[:max(0, remaining)]:
        sub = ((a.get("code") or {}).get("text")
               or ((a.get("code") or {}).get("coding") or [{}])[0].get("display", ""))
        out.append(_truncate(f"- {sub}  (bundle:{a.get('id')})"))
    return out


def _diag_report_lines(ex: ExtractedFacts | None) -> list[str]:
    if ex is None:
        return []
    out: list[str] = []
    for d in ex.diagnostic_reports[:MAX_ITEMS_PER_SECTION]:
        cite = _first_citation(d)
        out.append(_truncate(
            f"- [{d.modality} {d.body_site}]"
            f"{' on ' + d.performed_date if d.performed_date else ''}"
            f": {d.findings}{cite}"
        ))
    return out


def _first_citation(resource) -> str:
    cits = getattr(resource, "citations", None) or []
    if not cits:
        return ""
    c = cits[0]
    return f"  (cite: {c.document_id} p.{c.page})"


def _obs_value(q: float | None, unit: str | None, s: str | None) -> str:
    if q is not None:
        return f"{q}{unit or ''}"
    return s or "(no value)"


def _truncate(line: str) -> str:
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[: MAX_LINE_CHARS - 3] + "..."
