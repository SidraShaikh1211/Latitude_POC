"""Landing page — role picker for the PA prototype demo.

Run with:  .venv/bin/streamlit run frontend/app.py

The UI talks to the FastAPI backend via HTTP. Start it with `make run` in
another terminal (default http://127.0.0.1:8000). Override via the
STREAMLIT_API_BASE env var if needed.

Two roles, one backend:
  🩺 Doctor   — submit a new PA: upload PDF, fill form, watch the payer process it live
  🏥 Payer    — review incoming PAs: criteria tree, verdicts, narrative
"""

import os

import requests
import streamlit as st


API_BASE = os.environ.get("STREAMLIT_API_BASE", "http://127.0.0.1:8000")


def health_check() -> tuple[bool, str]:
    try:
        r = requests.get(f"{API_BASE}/health", timeout=3)
        if r.status_code == 200:
            return True, r.json().get("status", "?")
        return False, f"HTTP {r.status_code}"
    except Exception as e:
        return False, str(e)


def main() -> None:
    st.set_page_config(
        page_title="PA Prototype PA Prototype",
        page_icon=":hospital:",
        layout="wide",
    )

    st.title("PA Prototype — Prior Authorization Prototype")
    st.caption(
        "Payer-side decision support with a simulated provider front door. "
        "Da Vinci PAS Bundles in, ClaimResponse Bundles out. Citation-grounded "
        "FHIR extraction, deterministic policy selection, per-criterion "
        "adjudication, narrative review."
    )

    ok, msg = health_check()
    if ok:
        st.success(f"API connected at `{API_BASE}` — health: {msg}")
    else:
        st.error(
            f"API not reachable at `{API_BASE}` — {msg}. "
            "Start the backend with `make run` in another terminal."
        )

    st.markdown("---")
    st.subheader("Choose your role")

    col_doc, col_payer = st.columns(2)
    with col_doc:
        with st.container(border=True):
            st.markdown("### 🩺 Doctor")
            st.markdown(
                "Submit a new prior-auth request. Upload a patient's clinical PDF, "
                "fill in the service / coverage details, watch the payer process it "
                "live, and receive the determination + missing-info requests."
            )
            st.markdown(
                "- Upload any patient PDF\n"
                "- Form pre-validates against loaded policies (CPT, payer, state)\n"
                "- Preview the assembled FHIR Bundle before submitting\n"
                "- Live status updates every 2 seconds"
            )
            st.page_link(
                "pages/01_doctor_workspace.py",
                label="Open Doctor Workspace →",
                icon="🩺",
            )

    with col_payer:
        with st.container(border=True):
            st.markdown("### 🏥 Payer (Utilization Management)")
            st.markdown(
                "Review incoming PA submissions from any provider. See the full "
                "criteria tree with per-leaf verdicts, the Reviewer's narrative "
                "rationale, and the outbound PAS ClaimResponse Bundle."
            )
            st.markdown(
                "- Inbox of all received cases\n"
                "- Three-panel workspace: FHIR + Criteria tree + Determination\n"
                "- Click any criterion to see evidence + reasoning + policy quote\n"
                "- Same backend as the doctor surface"
            )
            st.page_link(
                "pages/02_payer_inbox.py",
                label="Open Payer Inbox →",
                icon="🏥",
            )

    st.markdown("---")
    st.subheader("Loaded policies")
    st.caption(
        "These are the policies the payer has on file. The selector matches "
        "incoming PA requests against `applies_to` to pick the right one."
    )
    try:
        r = requests.get(f"{API_BASE}/v1/policies", timeout=5)
        if r.status_code == 200:
            policies = r.json()
            for p in policies:
                with st.expander(f"{p['name']}  •  {p['policy_id']}  •  v{p['version']}"):
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Tree leaves", p["leaf_count"])
                    c2.metric("Exclusions", p["exclusion_count"])
                    c3.metric("Covered CPTs", len(p["cpt_codes"]))
                    st.write(
                        f"**Payer:** {p['payer_id']}  •  "
                        f"**LOB:** {', '.join(p['lines_of_business'])}  •  "
                        f"**States:** {', '.join(p['states'])}"
                    )
                    st.write(
                        f"**CPTs:** {', '.join(p['cpt_codes'])}  •  "
                        f"**Effective:** {p['effective_from']} → "
                        f"{p.get('effective_until') or 'present'}"
                    )
        else:
            st.warning("Could not load policies — is the API running?")
    except Exception as e:
        st.warning(f"Could not load policies: {e}")


if __name__ == "__main__":
    main()
else:
    main()
