"""Inbox page — list cases with status badges."""

import os

import pandas as pd
import requests
import streamlit as st


API_BASE = os.environ.get("STREAMLIT_API_BASE", "http://127.0.0.1:8000")


_STATUS_EMOJI = {
    "approved": "🟢",
    "denied": "🔴",
    "pended": "🟡",
    "needs_review": "🟣",
    "unknown": "⚪",
}


def main() -> None:
    st.set_page_config(page_title="Inbox — PA Cases", layout="wide")
    st.title("📥 Inbox — Prior Authorization Cases")

    try:
        r = requests.get(f"{API_BASE}/v1/cases", timeout=5)
        r.raise_for_status()
        cases = r.json()
    except Exception as e:
        st.error(f"Could not load cases: {e}")
        return

    if not cases:
        st.info(
            "No cases yet. Either:\n"
            "1. Run `make seed` to ingest the Smith PDF through the doctor flow, OR\n"
            "2. Open the Doctor Workspace and upload a patient PDF directly."
        )
        return

    rows = []
    for c in cases:
        rows.append({
            "": _STATUS_EMOJI.get(c["status"], "⚪"),
            "Case ID": c["case_id"],
            "Patient": c.get("patient_display") or "—",
            "CPT": c.get("cpt_code") or "—",
            "Payer": c.get("payer_id") or "—",
            "Policy": c.get("selected_policy_id") or "—",
            "Branch": c.get("branch") or "—",
            "Outcome": c.get("outcome") or "—",
        })
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, use_container_width=True)

    st.markdown("---")
    st.subheader("Open a case")
    case_ids = [c["case_id"] for c in cases]
    selected = st.selectbox("Case ID", case_ids, index=0)
    if st.button("Open Case Detail →", type="primary"):
        st.query_params["case_id"] = selected
        st.switch_page("pages/03_payer_case_detail.py")


main()
