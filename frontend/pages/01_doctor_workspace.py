"""Doctor workspace — PDF-only flow.

The doctor uploads a single clinical PDF. The system extracts patient
demographics, coverage info, and the requested service (including CPT
codes inferred from clinical context) automatically. Then the FHIR Bundle
is assembled and the payer pipeline runs.

Sections:
  A. Upload + Submit (no form fields)
  B. My submissions (polled every 2s)
     - In-progress cases show simplified status
     - Completed cases show outcome + Reviewer narrative + per-criterion
       citation cards ("Why this decision?")
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import requests
import streamlit as st


API_BASE = os.environ.get("STREAMLIT_API_BASE", "http://127.0.0.1:8000")


# ---------------------------------------------------------------------------
# Stage → doctor-facing label / progress
# ---------------------------------------------------------------------------

_DOCTOR_STAGE = {
    "extracting_metadata": ("📋 Reading your PDF",                     8),
    "received":            ("📨 Payer received request",              15),
    "parsing":             ("📨 Payer received request",              20),
    "intake":              ("🔍 Request analysis in progress",        35),
    "selecting":           ("🔍 Request analysis in progress",        45),
    "adjudicating":        ("🔍 Request analysis in progress",        70),
    "reviewing":           ("🔍 Request analysis in progress",        88),
    "building_response":   ("🔍 Request analysis in progress",        96),
    "complete":            ("✅ Complete",                           100),
    "failed":              ("❌ Submission failed",                  100),
}

_OUTCOME_LABEL = {
    "approve":            ("🟢 APPROVED",                      "green"),
    "deny":               ("🔴 DENIED",                        "red"),
    "pend":               ("🟡 PENDED — info requested",       "orange"),
    "needs_human_review": ("🟣 SENT TO HUMAN REVIEW",          "violet"),
}

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def fetch_case(case_id: str) -> dict | None:
    try:
        r = requests.get(f"{API_BASE}/v1/cases/{case_id}", timeout=5)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": str(e)}


def submit_pdf(pdf_bytes: bytes, filename: str) -> dict:
    files = {"pdf": (filename, pdf_bytes, "application/pdf")}
    r = requests.post(f"{API_BASE}/v1/doctor/submit", files=files, timeout=120)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Section A — Upload + Submit
# ---------------------------------------------------------------------------


def render_submit_section() -> None:
    st.markdown("##### Upload a clinical PDF")
    st.caption(
        "Drop a patient's clinical document (H&P, fax bundle, progress notes). "
        "The system will read the PDF and extract patient demographics, "
        "insurance info, requested CPT code (inferred from clinical context if "
        "not written explicitly), and ICD-10 diagnoses — no form fields needed."
    )

    pdf_file = st.file_uploader(
        "Clinical PDF",
        type="pdf",
        help=(
            "The metadata extractor (Claude) reads the PDF and infers CPT codes "
            "from descriptions like 'lumbar interlaminar ESI at L4/5' → 62323."
        ),
        label_visibility="collapsed",
    )

    submit = st.button(
        "📤 Submit to Payer",
        type="primary",
        disabled=pdf_file is None,
        use_container_width=False,
    )

    if submit and pdf_file is not None:
        try:
            with st.spinner(
                "📋 Reading your PDF and extracting metadata (this takes 10-15 seconds)..."
            ):
                resp = submit_pdf(pdf_file.getvalue(), pdf_file.name)
        except Exception as e:
            st.error(f"❌ Submission failed: {e}")
            return

        if resp.get("processing_stage") == "failed":
            # Metadata extraction couldn't fill required fields
            st.error("❌ " + (resp.get("error") or "Metadata extraction failed."))
            with st.expander("📋 What the extractor found (and what was missing)"):
                st.write(f"**Missing fields:** {', '.join(resp.get('missing_fields', []))}")
                st.write(f"**Extraction notes:** {resp.get('extraction_notes', '')}")
                st.json(resp.get("extracted_metadata") or {})
            return

        # Successful submission — record in session for live polling
        meta = resp.get("extracted_metadata") or {}
        patient = (meta.get("patient") or {})
        sr = (meta.get("service_request") or {})
        subs = st.session_state.setdefault("my_submissions", [])
        entry = {
            "case_id": resp["case_id"],
            "patient": (
                f"{patient.get('patient_given', '')} {patient.get('patient_family', '')}".strip()
                or "(unknown)"
            ),
            "cpt": sr.get("cpt_code"),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "metadata_cost_usd": resp.get("metadata_usage_cost_usd", 0),
            "bundle_entry_count": resp.get("bundle_entry_count"),
            "bundle_size_bytes": resp.get("bundle_size_bytes"),
            "extracted_metadata": meta,
            "extraction_notes": resp.get("extraction_notes", ""),
        }
        subs.insert(0, entry)
        # Stash bundle preview for the case-card expander
        st.session_state[f"bundle_preview_{resp['case_id']}"] = resp.get("bundle_preview")
        st.success(
            f"✅ Submitted to payer  ·  case_id `{resp['case_id']}`  ·  "
            f"CPT inferred: `{sr.get('cpt_code') or '?'}`  ·  "
            f"Bundle: {resp.get('bundle_entry_count')} entries, "
            f"{resp.get('bundle_size_bytes', 0):,} bytes"
        )


# ---------------------------------------------------------------------------
# Section B — case-card renderers
# ---------------------------------------------------------------------------


def _seconds_ago(iso: str) -> int:
    try:
        t = datetime.fromisoformat(iso)
        delta = datetime.now(timezone.utc) - t
        return int(delta.total_seconds())
    except Exception:
        return 0


def render_extracted_metadata_panel(entry: dict) -> None:
    meta = entry.get("extracted_metadata") or {}
    if not meta:
        return
    p = meta.get("patient") or {}
    c = meta.get("coverage") or {}
    s = meta.get("service_request") or {}
    with st.expander("📋 What we extracted from the PDF", expanded=False):
        col1, col2 = st.columns(2)
        with col1:
            st.markdown("**Patient**")
            st.write(f"Name: `{p.get('patient_given') or '?'} {p.get('patient_family') or '?'}`")
            st.write(f"DOB: `{p.get('patient_dob') or '?'}`  ·  Gender: `{p.get('patient_gender') or '?'}`")
            st.write(f"State: `{p.get('patient_state') or '?'}`")
            st.markdown("**Coverage**")
            st.write(f"Payer: `{c.get('payer_id') or '?'}` ({c.get('payer_display') or '?'})")
            st.write(f"Member ID: `{c.get('member_id') or '?'}`")
            st.write(f"LOB: `{c.get('line_of_business') or '?'}`  ·  Plan: `{c.get('plan_name') or '?'}`")
        with col2:
            st.markdown("**Service requested**")
            st.write(f"CPT: `{s.get('cpt_code') or '?'}`  ·  DOS: `{s.get('service_date') or '?'}`")
            st.write(f"_{s.get('cpt_display') or ''}_")
            st.write(f"Body site: `{s.get('body_site_display') or '?'}`")
            st.markdown("**ICD-10**")
            for icd in s.get("icd10_codes") or []:
                st.write(f"- `{icd.get('code')}` ({icd.get('kind')}) — {icd.get('display')}")
        notes = entry.get("extraction_notes")
        if notes:
            st.markdown("**Extraction reasoning (what was explicit vs inferred):**")
            st.caption(notes)


# (per-criterion drill-down view intentionally omitted from the doctor UI;
# the brief Reviewer narrative + missing-info is sufficient for the doctor.
# Full criteria-tree is on the payer's case detail page.)


def render_status_card(entry: dict, case: dict | None) -> None:
    case_id = entry["case_id"]
    with st.container(border=True):
        header_cols = st.columns([3, 2, 2])
        header_cols[0].markdown(f"**Case `{case_id}`**")
        header_cols[1].caption(
            f"Patient: {entry.get('patient') or '—'}  •  "
            f"CPT: `{entry.get('cpt') or '—'}`"
        )
        header_cols[2].caption(f"Submitted {_seconds_ago(entry['submitted_at'])}s ago")

        if case is None:
            st.warning("Case not yet visible to the payer (try refreshing in a few seconds).")
            return
        if case.get("_error"):
            st.warning(f"API error: {case['_error']}")
            return

        stage = case.get("processing_stage") or "extracting_metadata"
        label, pct = _DOCTOR_STAGE.get(stage, (f"Stage: {stage}", 0))

        if stage == "complete":
            outcome = case.get("outcome") or "needs_human_review"
            badge, color = _OUTCOME_LABEL.get(outcome, ("✅ Complete", "gray"))
            st.markdown(f":{color}[**{badge}**]")
            det = case.get("determination") or {}
            if det.get("narrative"):
                st.markdown("**Reviewer narrative:**")
                st.write(det["narrative"])
            mi = det.get("missing_info") or []
            if mi:
                st.markdown(f"**📨 Missing information requested ({len(mi)}):**")
                for m in mi:
                    with st.container(border=True):
                        st.markdown(f"**{m.get('id', '?')}** — {m.get('request', '')}")
                        st.caption(f"Criterion: `{m.get('criterion_id', '?')}`")

            # Show extracted metadata + bundle preview + payer detail link
            st.markdown("---")
            render_extracted_metadata_panel(entry)
            bundle = st.session_state.get(f"bundle_preview_{case_id}")
            if bundle:
                with st.expander("📦 Outbound FHIR Bundle that was sent to the payer"):
                    st.caption(
                        "This is the Da Vinci PAS Claim Bundle assembled from the extracted "
                        "metadata and POSTed to /fhir/Claim/$submit. In production, the doctor's "
                        "EHR would build this automatically."
                    )
                    st.json(bundle)
            if st.button("📋 View full payer detail", key=f"view_payer_{case_id}"):
                st.query_params["case_id"] = case_id
                st.switch_page("pages/03_payer_case_detail.py")

        elif stage == "failed":
            err = case.get("error_message") or "Unknown error"
            st.markdown(":red[**❌ Submission failed**]")
            st.error(err)
            render_extracted_metadata_panel(entry)

        else:
            # In-progress
            st.markdown(f"**{label}**")
            st.progress(pct / 100, text=f"{stage.replace('_', ' ').title()} — {pct}%")
            if stage in ("extracting_metadata",):
                st.caption("Reading the PDF and inferring CPT codes, patient info, and diagnoses.")
            elif stage in ("received", "parsing"):
                st.caption("The payer received your Bundle and is parsing it.")
            else:
                st.caption(
                    "The payer is analyzing your request — extracting clinical facts, "
                    "matching against policy criteria, drafting a narrative review."
                )
            render_extracted_metadata_panel(entry)


@st.fragment(run_every=2)
def render_submissions_list() -> None:
    subs = st.session_state.get("my_submissions", [])
    if not subs:
        st.info("Upload a PDF above to see live status here.")
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
        "Upload a patient's clinical PDF. The system extracts patient demographics, "
        "coverage info, ICD-10 diagnoses, and infers the requested CPT code from "
        "clinical context. Then your EHR's FHIR Bundle is assembled and sent to "
        "the payer. Watch live status below."
    )

    st.markdown("---")
    render_submit_section()

    st.markdown("---")
    st.subheader("📊 My submissions (live)")
    render_submissions_list()


main()
