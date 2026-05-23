"""Case Detail — three-panel workspace.

Left:  Structured FHIR facts (intake-extracted + Bundle-derived) with
       [p.N] anchors expanding to the cited source excerpt.
Right: Criteria tree with verdict badges; expanders show patient evidence
       + reasoning + the policy quote that drove the verdict.
Below: Determination outcome + narrative + missing-info + raw ClaimResponse.
"""

import json
import os
from typing import Any

import requests
import streamlit as st


API_BASE = os.environ.get("STREAMLIT_API_BASE", "http://127.0.0.1:8000")


_VERDICT_BADGE = {
    "met": ("✅", "green"),
    "not_met": ("❌", "red"),
    "unclear": ("🟡", "orange"),
    "not_documented": ("📭", "gray"),
}

_OUTCOME_BADGE = {
    "approve": ("🟢 APPROVED", "green"),
    "deny": ("🔴 DENIED", "red"),
    "pend": ("🟡 PENDED — info requested", "orange"),
    "needs_human_review": ("🟣 NEEDS HUMAN REVIEW", "violet"),
}


@st.cache_data(ttl=60)
def fetch_case(case_id: str) -> dict[str, Any] | None:
    r = requests.get(f"{API_BASE}/v1/cases/{case_id}", timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=300)
def fetch_policy(policy_id: str) -> dict[str, Any] | None:
    r = requests.get(f"{API_BASE}/v1/policies/{policy_id}", timeout=10)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _badge(text: str, color: str) -> None:
    st.markdown(f":{color}[**{text}**]")


def _render_patient(facts: dict | None) -> None:
    st.markdown("**👤 Patient**")
    if not facts or not facts.get("patient"):
        st.caption("No patient resource extracted.")
        return
    p = facts["patient"]
    cols = st.columns(4)
    cols[0].write(f"**Name:** {p.get('given') or '?'} {p.get('family') or '?'}")
    cols[1].write(f"**DOB:** {p.get('birth_date') or '?'}")
    cols[2].write(f"**Gender:** {p.get('gender') or '?'}")
    cols[3].write(f"**MRN:** {p.get('mrn') or '?'}")
    _render_citations(p.get("citations"))


def _render_resource_list(title: str, items: list[dict], primary_field: str, secondary_field: str = "") -> None:
    if not items:
        return
    st.markdown(f"**{title}** ({len(items)})")
    for it in items:
        primary = it.get(primary_field) or "?"
        secondary = it.get(secondary_field) if secondary_field else None
        line = f"- {primary}"
        if secondary:
            line += f"  — _{secondary}_"
        # Add citation page anchors inline
        cits = it.get("citations") or []
        pages = sorted({c.get("page") for c in cits if c.get("page")})
        if pages:
            line += "  " + " ".join(f"[p.{p}]" for p in pages)
        with st.expander(line):
            _render_citations(cits)


def _render_citations(cits: list[dict] | None) -> None:
    if not cits:
        st.caption("_no citations_")
        return
    for c in cits:
        st.markdown(
            f"  - **p.{c.get('page')}** · {c.get('section') or 'unsectioned'} "
            f"· conf={c.get('extraction_confidence', 0):.2f}"
        )
        st.code(c.get("quote", ""), language=None)


def _render_fhir_panel(facts: dict | None) -> None:
    if not facts:
        st.warning("No intake-extracted facts (intake may have failed or been skipped).")
        return
    _render_patient(facts)
    st.markdown("---")
    _render_resource_list("🩺 Conditions", facts.get("conditions") or [], "display", "icd10_code")
    _render_resource_list("📊 Observations", facts.get("observations") or [], "code_display", "value_string")
    _render_resource_list("💊 Medications", facts.get("medications") or [], "medication_name", "dose")
    _render_resource_list("🛠 Procedures", facts.get("procedures") or [], "display", "cpt_code")
    _render_resource_list("⚠️  Allergies", facts.get("allergies") or [], "substance", "reaction")
    _render_resource_list("🧪 Diagnostic Reports", facts.get("diagnostic_reports") or [], "findings", "modality")


def _flatten_tree(node: dict, depth: int = 0) -> list[tuple[int, dict]]:
    out = [(depth, node)]
    for c in node.get("children", []) or []:
        out.extend(_flatten_tree(c, depth + 1))
    return out


def _render_criteria_panel(policy: dict | None, evaluation: dict | None) -> None:
    if not policy:
        st.warning("Policy not loaded.")
        return
    leaf_verdicts = (evaluation or {}).get("leaf_verdicts") or {}
    exclusion_verdicts = (evaluation or {}).get("exclusion_verdicts") or {}

    st.markdown(f"**📋 {policy['name']}**")
    st.caption(f"{policy['policy_id']} v{policy['version']} · payer={policy['payer_id']}")
    st.markdown("---")
    st.markdown("### Criteria tree")
    for depth, node in _flatten_tree(policy["criteria"]):
        indent = "&nbsp;&nbsp;&nbsp;&nbsp;" * depth
        if node["type"] == "internal":
            st.markdown(f"{indent}**[{node['operator']}] {node['id'].split('.')[-1]}** — {node['description']}", unsafe_allow_html=True)
        else:
            v = leaf_verdicts.get(node["id"]) or {}
            verdict = v.get("verdict") or "?"
            emoji, color = _VERDICT_BADGE.get(verdict, ("?", "gray"))
            conf = v.get("confidence")
            conf_str = f"  · conf {conf:.2f}" if isinstance(conf, (int, float)) else ""
            with st.expander(f"{indent}{emoji} **{verdict}**{conf_str}  · {node['id'].split('.')[-1]}", expanded=False):
                st.markdown(f"_{node['description']}_")
                cit = node.get("policy_citation") or {}
                st.markdown(f"**Policy citation (p.{cit.get('page')}, {cit.get('section') or 'unsectioned'}):**")
                st.code(cit.get("quote") or "", language=None)
                if v.get("reasoning"):
                    st.markdown("**Reasoning:**")
                    st.write(v["reasoning"])
                if v.get("patient_evidence"):
                    st.markdown("**Patient evidence cited:**")
                    for e in v["patient_evidence"]:
                        st.markdown(
                            f"- _{e.get('fact_type') or 'unknown'}_ · {e.get('value_summary') or ''}"
                            + (f"  [p.{e.get('page')}]" if e.get("page") else "")
                        )
                        if e.get("quote"):
                            st.code(e["quote"], language=None)
                if v.get("missing_info"):
                    st.markdown("**Missing info:**")
                    for m in v["missing_info"]:
                        st.markdown(f"- {m}")

    if policy.get("exclusions"):
        st.markdown("### Exclusions")
        for ex in policy["exclusions"]:
            v = exclusion_verdicts.get(ex["id"]) or {}
            verdict = v.get("verdict") or "?"
            emoji, color = _VERDICT_BADGE.get(verdict, ("?", "gray"))
            label = f"{emoji} **{verdict}**  · {ex['id']}: {ex['description'][:80]}"
            with st.expander(label):
                cit = ex.get("policy_citation") or {}
                st.markdown(f"**Policy citation (p.{cit.get('page')}):**")
                st.code(cit.get("quote") or "", language=None)
                if v.get("reasoning"):
                    st.markdown("**Adjudicator reasoning:**")
                    st.write(v["reasoning"])


def _render_determination(case: dict) -> None:
    det = case.get("determination") or {}
    outcome = case.get("outcome") or "?"
    label, color = _OUTCOME_BADGE.get(outcome, ("?", "gray"))
    _badge(label, color)
    if det.get("rationale"):
        st.caption(det["rationale"])
    if det.get("triggered_exclusions"):
        st.markdown(f"**Triggered exclusions:** {', '.join(det['triggered_exclusions'])}")
    if det.get("narrative"):
        st.markdown("#### Reviewer narrative")
        st.write(det["narrative"])
    if det.get("missing_info"):
        st.markdown(f"#### Missing information ({len(det['missing_info'])})")
        for mi in det["missing_info"]:
            with st.container(border=True):
                st.markdown(f"**{mi.get('id', '?')}** · criterion: `{mi.get('criterion_id', '?')}`")
                st.write(mi.get("request") or "")

    st.markdown("#### Action")
    c1, c2, c3, c4 = st.columns(4)
    c1.button("✅ Approve", disabled=True, help="UI demo — not wired to a real workflow.")
    c2.button("❌ Deny", disabled=True)
    c3.button("📨 Send info request", disabled=True)
    c4.button("👤 Send to medical director", disabled=True)


def main() -> None:
    st.set_page_config(page_title="Case Detail", layout="wide")
    qp = st.query_params
    case_id = qp.get("case_id")
    if not case_id:
        st.error("No case_id in query parameters. Open a case from the Inbox.")
        st.page_link("pages/02_payer_inbox.py", label="← Back to Inbox", icon="📥")
        return

    case = fetch_case(case_id)
    if not case:
        st.error(f"Case `{case_id}` not found.")
        return

    st.title(f"Case · {case_id}")
    st.caption(
        f"Patient: {case.get('patient_display') or '—'}  •  "
        f"CPT: {case.get('cpt_code') or '—'}  •  "
        f"Payer: {case.get('payer_id') or '—'}  •  "
        f"Policy: {case.get('selected_policy_id') or '—'}  •  "
        f"Branch: {case.get('branch') or '—'}"
    )
    if case.get("policy_selection"):
        sel = case["policy_selection"]
        st.caption(f"Selection: {sel.get('status')} — {sel.get('selection_reason')}")

    policy = (
        fetch_policy(case["selected_policy_id"])
        if case.get("selected_policy_id") else None
    )

    # Three-panel layout: FHIR | Criteria | Determination (full width below)
    left, right = st.columns([5, 7])
    with left:
        st.subheader("🗂 Structured FHIR (intake-extracted)")
        _render_fhir_panel(case.get("extracted_facts"))
    with right:
        st.subheader("📋 Criteria evaluation")
        _render_criteria_panel(policy, case.get("criteria_evaluation"))

    st.markdown("---")
    st.subheader("⚖️ Determination")
    _render_determination(case)

    with st.expander("📤 Raw outbound PAS ClaimResponse Bundle"):
        st.json(case.get("pas_response_bundle") or {})


main()
