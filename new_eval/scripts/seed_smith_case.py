"""Construct the David Smith Da Vinci PAS Claim Bundle and (optionally) run
the end-to-end pipeline.

This script is the SOURCE OF TRUTH for the Smith fixture
(tests/fixtures/smith_claim_bundle.json). Re-run any time the input PDF or
metadata changes. The bundle-building logic lives in
`app/pas/bundle_constructor.py` so the same builders are reused by the
doctor-submit REST endpoint.

Usage:
    .venv/bin/python -m scripts.seed_smith_case                  # fixture + e2e run
    .venv/bin/python -m scripts.seed_smith_case --fixture-only   # just write the fixture
    .venv/bin/python -m scripts.seed_smith_case --case-id smith-001
"""

from __future__ import annotations

import json

from app.pas.bundle_constructor import build_pas_bundle, smith_submission
from app.settings import PROJECT_ROOT


SMITH_PDF = PROJECT_ROOT / "clinical_pdfs" / "David_Smith_Clinical.pdf"
FIXTURE_OUT = PROJECT_ROOT / "tests" / "fixtures" / "smith_claim_bundle.json"


def build_bundle(case_id: str = "smith-001") -> dict:
    if not SMITH_PDF.exists():
        raise FileNotFoundError(f"Smith PDF not found at {SMITH_PDF}")
    pdf_bytes = SMITH_PDF.read_bytes()
    submission = smith_submission(pdf_bytes)
    bundle, _ = build_pas_bundle(submission, case_id=case_id)
    return bundle


async def _evaluate_and_persist(bundle: dict, case_id: str) -> None:
    """Run the end-to-end pipeline on the Smith bundle and persist to SQLite.
    Requires ANTHROPIC_API_KEY to be set."""
    from app.api.cases import _persist
    from app.db.engine import init_db
    from app.orchestrator import evaluate_pa_case

    await init_db()
    print(f"\nRunning end-to-end pipeline on Smith case (this takes ~3-4 minutes)...")
    run = await evaluate_pa_case(bundle, run_intake_on_documents=True, case_id=case_id)
    await _persist(run)

    print(f"\n=== SMITH CASE — END-TO-END RESULT ===")
    print(f"  case_id:        {run.case_id}")
    print(f"  selection:      {run.selection.status} → {run.selection.selected_policy_id} ({run.selection.branch})")
    if run.adjudication:
        v_counts = {"met": 0, "not_met": 0, "unclear": 0, "not_documented": 0}
        for v in run.adjudication.leaf_verdicts.values():
            v_counts[v.verdict] = v_counts.get(v.verdict, 0) + 1
        print(f"  leaf verdicts:  met={v_counts['met']}  not_met={v_counts['not_met']}  unclear={v_counts['unclear']}  not_documented={v_counts['not_documented']}")
        print(f"  cost:           ${run.adjudication.total_usage.cost_usd:.4f}")
        print(f"  cache_read:     {run.adjudication.total_usage.cache_read_tokens:,} tokens")
    print(f"  outcome:        {run.determination.outcome if run.determination else '?'}")
    if run.reviewer:
        print(f"  missing_info:   {len(run.reviewer.output.missing_info)} requests")
    print(f"\nCase persisted to SQLite. View in Streamlit at http://localhost:8501")


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Seed the Smith case: build fixture + (optionally) run pipeline + persist.")
    parser.add_argument("--fixture-only", action="store_true",
                        help="Build the Smith Bundle JSON fixture only; skip the pipeline run.")
    parser.add_argument("--case-id", default="smith-001", help="Case ID for persistence.")
    args = parser.parse_args()

    FIXTURE_OUT.parent.mkdir(parents=True, exist_ok=True)
    bundle = build_bundle(case_id=args.case_id)
    FIXTURE_OUT.write_text(json.dumps(bundle, indent=2))
    print(f"Wrote {FIXTURE_OUT.relative_to(PROJECT_ROOT)}")
    print(f"  Bundle.entry count: {len(bundle['entry'])}")
    print(f"  Claim.diagnosis count: {len(bundle['entry'][0]['resource']['diagnosis'])}")
    print(f"  CPT: 62323  | DOS: 2026-04-08  | Payer: molina  | LOB: medicaid  | State: NY")
    print(f"  Includes Smith PDF as DocumentReference + Binary (base64-encoded)")

    if args.fixture_only:
        print("\n--fixture-only specified; skipping pipeline run.")
        return

    from app.settings import settings
    if not settings.anthropic_api_key:
        print("\nANTHROPIC_API_KEY not set; skipping pipeline run.")
        print("To run end-to-end:  export ANTHROPIC_API_KEY=sk-... && make seed")
        return

    import asyncio
    asyncio.run(_evaluate_and_persist(bundle, args.case_id))


if __name__ == "__main__":
    main()
