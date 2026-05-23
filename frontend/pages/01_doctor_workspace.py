"""Doctor workspace — upload a clinical PDF, fill the form, watch it process.

Sections:
  A. Submit form (PDF upload + patient + coverage + service)
  B. My submissions (live status, polled every 2s via st.fragment)

The "My submissions" list is session-scoped (st.session_state) — refreshing
the browser clears it. This is intentional: the doctor's view is their
ephemeral inbox; the payer's view (separate page) is the persistent record.

Doctor-facing status maps multiple internal pipeline stages into 3 buckets:
  📨 Payer received request           (received, parsing)
  🔍 Request analysis in progress     (intake, selecting, adjudicating, reviewing, building_response)
  ✅ Complete (with outcome + narrative + missing-info)
  ❌ Failed (with error excerpt)
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timezone
from typing import Any

import requests
import streamlit as st


API_BASE = os.environ.get("STREAMLIT_API_BASE", "http://127.0.0.1:8000")


# ---------------------------------------------------------------------------
# Stage → doctor-facing label / progress mapping
# ---------------------------------------------------------------------------

_DOCTOR_STAGE = {
    "received":          ("📨 Payer received request",       5),
    "parsing":           ("📨 Payer received request",      10),
    "intake":            ("🔍 Request analysis in progress", 25),
    "selecting":         ("🔍 Request analysis in progress", 35),
    "adjudicating":      ("🔍 Request analysis in progress", 65),
    "reviewing":         ("🔍 Request analysis in progress", 85),
    "building_response": ("🔍 Request analysis in progress", 95),
    "complete":          ("✅ Complete",                    100),
    "failed":            ("❌ Submission failed",           100),
}

_OUTCOME_LABEL = {
    "approve":             ("🟢 APPROVED",       "green"),
    "deny":                ("🔴 DENIED",         "red"),
    "pend":                ("🟡 PENDED — info requested", "orange"),
    "needs_human_review":  ("🟣 SENT TO HUMAN REVIEW",     "violet"),
}


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def fetch_policies() -> list[dict]:
    try:
        r = requests.get(f"{API_BASE}/v1/policies", timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


def fetch_case(case_id: str) -> dict | None:
    try:
        r = requests.get(f"{API_BASE}/v1/cases/{case_id}", timeout=5)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def submit_case(metadata: dict, pdf_bytes: bytes, filename: str) -> dict:
    files = {"pdf": (filename, pdf_bytes, "application/pdf")}
    data = {"metadata": json.dumps(metadata)}
    r = requests.post(f"{API_BASE}/v1/doctor/submit", files=files, data=data, timeout=30)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Form helpers
# ---------------------------------------------------------------------------


def _policy_dropdown_options(policies: list[dict]) -> dict[str, dict]:
    """Build {label: policy_dict} for the payer/CPT/state dropdowns."""
    return {f"{p['name']} ({p['payer_id']})": p for p in policies}


def _smith_defaults() -> dict[str, Any]:
    return {
        "patient_given": "David",
        "patient_family": "Smith",
        "patient_dob": "1975-11-02",
        "patient_gender": "male",
        "patient_state": "NY",
        "payer_id": "molina",
        "payer_display": "Molina Healthcare of New York",
        "member_id": "KF464W",
        "line_of_business": "medicaid",
        "plan_name": "Molina Medicaid NY",
        "cpt_code": "62323",
        "cpt_display": "Lumbar interlaminar epidural steroid injection with imaging guidance",
        "service_date": "2026-04-08",
        "icd10_primary_code": "M54.16",
        "icd10_primary_display": "Radiculopathy, lumbar region",
        "icd10_secondary_text": "M79.18 — Other myalgia\nM47.816 — Spondylosis without myelopathy or radiculopathy, lumbar region",
        "body_site_display": "Lumbar — L4/5 vs L5/S1",
        "provider_org_name": "NYU Langone Health — Center for the Study and Treatment of Pain",
    }


def _parse_secondary_icd10(text: str) -> list[dict]:
    """Parse one-per-line ICD-10 entries. Format: 'CODE' or 'CODE — display'."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # Split on em-dash or hyphen
        if "—" in line:
            code, _, disp = line.partition("—")
        elif " - " in line:
            code, _, disp = line.partition(" - ")
        else:
            code, disp = line, line
        out.append({
            "code": code.strip(),
            "display": disp.strip() or code.strip(),
            "kind": "secondary",
        })
    return out


# ---------------------------------------------------------------------------
# Submit form
# ---------------------------------------------------------------------------


def render_submit_form(policies: list[dict]) -> None:
    if not policies:
        st.warning("No policies loaded by the payer — submissions will return `no_match`.")

    # "Load Smith demo data" button populates session_state
    cols = st.columns([3, 2, 2])
    cols[0].markdown("##### Submit a new prior authorization")
    if cols[2].button("🧪 Load Smith demo data", help="Pre-fill the form with David Smith case values"):
        for k, v in _smith_defaults().items():
            st.session_state[f"form_{k}"] = v
        st.rerun()

    pdf_file = st.file_uploader(
        "📎 Clinical PDF (H&P, fax bundle, progress notes)",
        type="pdf",
        help="The payer's intake agent will extract FHIR resources from this PDF with verbatim citations.",
    )

    with st.form("doctor_submit_form", clear_on_submit=False):
        # PATIENT
        st.markdown("###### 👤 Patient")
        c1, c2, c3, c4 = st.columns(4)
        patient_given = c1.text_input("Given name", key="form_patient_given", placeholder="David")
        patient_family = c2.text_input("Family name", key="form_patient_family", placeholder="Smith")
        patient_dob = c3.text_input("DOB (YYYY-MM-DD)", key="form_patient_dob", placeholder="1975-11-02")
        patient_gender = c4.selectbox(
            "Gender",
            ["male", "female", "other", "unknown"],
            index=["male", "female", "other", "unknown"].index(st.session_state.get("form_patient_gender", "male"))
                  if st.session_state.get("form_patient_gender") in ("male","female","other","unknown") else 0,
            key="form_patient_gender",
        )

        # COVERAGE
        st.markdown("###### 🏥 Coverage")
        c1, c2, c3, c4 = st.columns(4)
        # Payer dropdown — derived from loaded policies
        payer_ids = sorted({p["payer_id"] for p in policies}) or ["(no payers loaded)"]
        cur_payer = st.session_state.get("form_payer_id", payer_ids[0] if payer_ids else "")
        payer_id = c1.selectbox(
            "Payer",
            payer_ids,
            index=payer_ids.index(cur_payer) if cur_payer in payer_ids else 0,
            key="form_payer_id",
        )
        payer_display = c2.text_input("Payer display name", key="form_payer_display",
                                       placeholder="Molina Healthcare of New York")
        member_id = c3.text_input("Member ID", key="form_member_id", placeholder="KF464W")
        # LOB
        lobs = sorted({lob for p in policies for lob in p["lines_of_business"]}) or ["medicaid"]
        cur_lob = st.session_state.get("form_line_of_business", lobs[0])
        line_of_business = c4.selectbox(
            "Line of business",
            lobs,
            index=lobs.index(cur_lob) if cur_lob in lobs else 0,
            key="form_line_of_business",
        )
        c1, c2 = st.columns(2)
        # State
        states = sorted({s for p in policies for s in p["states"]}) or ["NY"]
        cur_state = st.session_state.get("form_patient_state", states[0])
        patient_state = c1.selectbox(
            "Patient state",
            states,
            index=states.index(cur_state) if cur_state in states else 0,
            key="form_patient_state",
        )
        plan_name = c2.text_input("Plan name (e.g. 'Molina Medicaid NY')",
                                   key="form_plan_name", placeholder="Molina Medicaid NY")

        # SERVICE REQUEST
        st.markdown("###### 🛠 Service requested")
        c1, c2, c3 = st.columns([1, 1, 2])
        # CPT — flatten all CPTs from loaded policies for hint, but allow free text
        all_cpts = sorted({c for p in policies for c in p["cpt_codes"]})
        cpt_help = f"Examples (must match a loaded policy): {', '.join(all_cpts)}" if all_cpts else ""
        cpt_code = c1.text_input("CPT code", key="form_cpt_code", placeholder="62323", help=cpt_help)
        service_date = c2.text_input("Date of service (YYYY-MM-DD)",
                                      key="form_service_date",
                                      placeholder=date.today().isoformat())
        cpt_display = c3.text_input("CPT description (free text)",
                                     key="form_cpt_display",
                                     placeholder="Lumbar interlaminar ESI with imaging guidance")

        # ICD-10 — split into primary + secondary text area
        st.markdown("###### 🩺 Diagnoses (ICD-10)")
        c1, c2 = st.columns(2)
        icd10_primary_code = c1.text_input("Primary diagnosis code",
                                            key="form_icd10_primary_code",
                                            placeholder="M54.16")
        icd10_primary_display = c2.text_input("Primary diagnosis description",
                                               key="form_icd10_primary_display",
                                               placeholder="Radiculopathy, lumbar region")
        icd10_secondary_text = st.text_area(
            "Secondary diagnoses (one per line, format: `CODE — display`)",
            key="form_icd10_secondary_text",
            placeholder="M79.18 — Other myalgia",
            height=100,
        )

        # PROVIDER / BODY SITE
        c1, c2 = st.columns(2)
        body_site_display = c1.text_input("Body site (free text)",
                                           key="form_body_site_display",
                                           placeholder="Lumbar — L4/5 vs L5/S1")
        provider_org_name = c2.text_input("Submitting provider org",
                                           key="form_provider_org_name",
                                           placeholder="Submitting Provider")

        # Buttons
        st.markdown("---")
        b1, b2, _ = st.columns([1, 1, 4])
        preview_clicked = b1.form_submit_button("👁 Preview Bundle", use_container_width=True)
        submit_clicked = b2.form_submit_button("📤 Submit to Payer",
                                                type="primary",
                                                use_container_width=True)

    # Process form actions outside the form
    if preview_clicked or submit_clicked:
        if pdf_file is None:
            st.error("❌ Please upload a clinical PDF before previewing or submitting.")
            return
        # Validate required fields
        required = {
            "patient_given": patient_given,
            "patient_family": patient_family,
            "patient_dob": patient_dob,
            "payer_id": payer_id,
            "member_id": member_id,
            "cpt_code": cpt_code,
            "service_date": service_date,
            "icd10_primary_code": icd10_primary_code,
        }
        missing = [k for k, v in required.items() if not (v or "").strip()]
        if missing:
            st.error(f"❌ Missing required fields: {', '.join(missing)}")
            return

        # Build the metadata payload
        icd10_codes = [{
            "code": icd10_primary_code.strip(),
            "display": (icd10_primary_display or icd10_primary_code).strip(),
            "kind": "primary",
        }]
        icd10_codes.extend(_parse_secondary_icd10(icd10_secondary_text))
        metadata = {
            "patient_given": patient_given,
            "patient_family": patient_family,
            "patient_dob": patient_dob,
            "patient_gender": patient_gender,
            "patient_state": patient_state,
            "payer_id": payer_id,
            "payer_display": payer_display or payer_id.title(),
            "member_id": member_id,
            "line_of_business": line_of_business,
            "plan_name": plan_name or payer_id,
            "cpt_code": cpt_code,
            "cpt_display": cpt_display or cpt_code,
            "service_date": service_date,
            "icd10_codes": icd10_codes,
            "body_site_display": body_site_display,
            "provider_org_name": provider_org_name or "Submitting Provider",
        }

        if preview_clicked:
            # Show the Bundle preview without submitting
            try:
                # Call submit endpoint with a stash flag would be ideal, but for
                # simplicity we just build the bundle client-side via the same
                # endpoint and discard the response (it'll process in background).
                # Better: a dedicated /v1/doctor/preview endpoint. For POC, we
                # piggyback on submit and let the user see what would happen.
                # → Actually we'll just preview the metadata for now.
                st.info("Bundle preview shows the metadata that will be assembled. "
                        "The full Bundle (with base64 PDF) is generated server-side on submit.")
                with st.expander("📋 Metadata that will be assembled into a PAS Bundle", expanded=True):
                    st.json(metadata)
                with st.expander("📎 PDF attachment"):
                    st.write(f"File: `{pdf_file.name}`  ·  Size: {len(pdf_file.getvalue()):,} bytes")
            except Exception as e:
                st.error(f"Preview failed: {e}")
            return

        if submit_clicked:
            try:
                with st.spinner("Assembling FHIR Bundle and submitting to payer..."):
                    resp = submit_case(metadata, pdf_file.getvalue(), pdf_file.name)
            except Exception as e:
                st.error(f"❌ Submission failed: {e}")
                return

            # Record submission for the live-status list
            subs = st.session_state.setdefault("my_submissions", [])
            entry = {
                "case_id": resp["case_id"],
                "patient": f"{patient_given} {patient_family}".strip(),
                "cpt": cpt_code,
                "payer": payer_id,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "bundle_entry_count": resp.get("bundle_entry_count"),
                "bundle_size_bytes": resp.get("bundle_size_bytes"),
            }
            # Prepend so the newest is at the top
            subs.insert(0, entry)
            # Stash the bundle preview for the expander in the status card
            st.session_state[f"bundle_preview_{resp['case_id']}"] = resp.get("bundle_preview")
            st.success(
                f"✅ Submitted to payer  ·  case_id `{resp['case_id']}`  ·  "
                f"Bundle: {resp.get('bundle_entry_count')} entries, "
                f"{resp.get('bundle_size_bytes', 0):,} bytes"
            )


# ---------------------------------------------------------------------------
# My submissions list (polled)
# ---------------------------------------------------------------------------


def _seconds_ago(iso: str) -> int:
    try:
        t = datetime.fromisoformat(iso)
        delta = datetime.now(timezone.utc) - t
        return int(delta.total_seconds())
    except Exception:
        return 0


def render_status_card(entry: dict, case: dict | None) -> None:
    case_id = entry["case_id"]
    with st.container(border=True):
        header_cols = st.columns([3, 2, 2])
        header_cols[0].markdown(f"**Case `{case_id}`**")
        header_cols[1].caption(
            f"Patient: {entry.get('patient') or '—'}  •  CPT: {entry.get('cpt') or '—'}"
        )
        header_cols[2].caption(f"Submitted {_seconds_ago(entry['submitted_at'])}s ago")

        if case is None:
            st.warning("Could not load case status from payer.")
            return
        if case.get("_error"):
            st.warning(f"API error: {case['_error']}")
            return

        stage = case.get("processing_stage") or "received"
        label, pct = _DOCTOR_STAGE.get(stage, (f"Stage: {stage}", 0))

        if stage == "complete":
            outcome = case.get("outcome") or "needs_human_review"
            label, color = _OUTCOME_LABEL.get(outcome, ("✅ Complete", "gray"))
            st.markdown(f":{color}[**{label}**]")
            det = case.get("determination") or {}
            if det.get("narrative"):
                st.markdown("**Reviewer narrative:**")
                st.write(det["narrative"])
            mi = det.get("missing_info") or []
            if mi:
                st.markdown(f"**Missing information ({len(mi)}):**")
                for m in mi:
                    st.markdown(f"- **{m.get('id', '?')}** — {m.get('request', '')[:300]}")
            # Link to payer-side detail
            cols = st.columns([2, 2, 4])
            if cols[0].button(f"📋 View full payer detail", key=f"view_payer_{case_id}"):
                st.query_params["case_id"] = case_id
                st.switch_page("pages/03_payer_case_detail.py")
            # Bundle preview
            bundle = st.session_state.get(f"bundle_preview_{case_id}")
            if bundle:
                with st.expander("📦 Outbound FHIR Bundle that the EHR sent to the payer"):
                    st.caption("This is the Da Vinci PAS Claim Bundle the doctor's EHR assembled and POSTed to /fhir/Claim/$submit.")
                    st.json(bundle)

        elif stage == "failed":
            err = case.get("error_message") or "Unknown error"
            st.markdown(":red[**❌ Submission failed**]")
            st.error(err)

        else:
            # In-progress (received / parsing / intake / selecting / adjudicating / reviewing / building_response)
            st.markdown(f"**{label}**")
            st.progress(pct / 100, text=f"{stage.replace('_', ' ').title()} — {pct}%")
            if stage in ("received", "parsing"):
                st.caption("The payer has received your Bundle and is parsing it.")
            else:
                st.caption(
                    "The payer is analyzing your request — extracting clinical facts, "
                    "matching against policy criteria, and drafting a narrative review."
                )


@st.fragment(run_every=2)
def render_submissions_list() -> None:
    subs = st.session_state.get("my_submissions", [])
    if not subs:
        st.info("Submit a case above to see live status here.")
        return
    for entry in subs:
        case = fetch_case(entry["case_id"])
        render_status_card(entry, case)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Doctor Workspace", layout="wide")
    st.title("🩺 Doctor Workspace")
    st.caption(
        "Upload a patient's clinical PDF, fill the service + coverage details, "
        "and submit. Behind the scenes, your EHR assembles a Da Vinci PAS Claim "
        "Bundle and POSTs it to the payer's `/fhir/Claim/$submit` endpoint. "
        "Watch live status below."
    )

    policies = fetch_policies()

    # Section A — submit form
    st.markdown("---")
    render_submit_form(policies)

    # Section B — live submissions
    st.markdown("---")
    st.subheader("📊 My submissions (live)")
    render_submissions_list()


main()
