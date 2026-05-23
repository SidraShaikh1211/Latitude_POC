"""Seed the David Smith case through the REAL doctor flow.

Reads `clinical_pdfs/David_Smith_Clinical.pdf`, runs the metadata
extractor on it (just like POST /v1/doctor/submit does), assembles the
Bundle from extracted metadata, runs the full payer pipeline, and
persists the case to SQLite.

No hardcoded Smith data anywhere. Same code path that a doctor's upload
takes. This script exists so you can pre-populate the payer's inbox for
demos without clicking through the UI.

Usage:
    .venv/bin/python -m scripts.seed_smith_case
    .venv/bin/python -m scripts.seed_smith_case --case-id smith-001
    .venv/bin/python -m scripts.seed_smith_case --pdf path/to/some.pdf
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.settings import PROJECT_ROOT, settings


DEFAULT_SMITH_PDF = PROJECT_ROOT / "clinical_pdfs" / "David_Smith_Clinical.pdf"


async def _seed(pdf_path: Path, case_id: str) -> None:
    """Run the doctor flow on a PDF and persist the resulting case."""
    from app.api.cases import _persist
    from app.db.engine import init_db
    from app.extraction.metadata import extract_submission_metadata, missing_required_fields
    from app.extraction.pdf import extract_pdf
    from app.orchestrator import evaluate_pa_case
    from app.pas.bundle_constructor import build_pas_bundle

    await init_db()

    print(f"\nReading PDF: {pdf_path.relative_to(PROJECT_ROOT)}")
    pdf_bytes = pdf_path.read_bytes()
    doc = extract_pdf(pdf_path, document_id=f"upload-{case_id}")
    print(f"  PDF parsed: {doc.page_count} pages, {len(doc.full_text):,} chars")

    print(f"\n→ Running metadata extractor (Claude) ...")
    meta_result = await extract_submission_metadata(
        doc, pdf_bytes=pdf_bytes, pdf_filename=pdf_path.name,
    )
    print(f"  Metadata extraction done  ·  cost ${meta_result.usage.cost_usd:.4f}")

    sub = meta_result.submission
    print(f"  Patient:  {sub.patient_given} {sub.patient_family}  ·  DOB {sub.patient_dob}")
    print(f"  Coverage: {sub.payer_id} ({sub.payer_display})  ·  member {sub.member_id}")
    print(f"  Service:  CPT {sub.cpt_code}  ·  DOS {sub.service_date}")
    icd_strs = [f"{c.code} ({c.kind})" for c in sub.icd10_codes]
    print(f"  ICD-10:   {', '.join(icd_strs)}")

    missing = missing_required_fields(meta_result.metadata)
    if missing:
        print(f"\n✗ Metadata extraction missing required fields: {missing}")
        print(f"  Extraction notes: {meta_result.notes}")
        return

    print(f"\n→ Building Da Vinci PAS Bundle ...")
    bundle, _ = build_pas_bundle(sub, case_id=case_id)
    print(f"  Bundle has {len(bundle['entry'])} entries")

    print(f"\n→ Running payer pipeline (intake + selector + adjudicator + reviewer) ...")
    print(f"  This takes ~3-4 minutes and costs ~$1-2 in API.")
    run = await evaluate_pa_case(bundle, run_intake_on_documents=True, case_id=case_id)
    await _persist(run)

    print(f"\n=== END-TO-END RESULT ===")
    print(f"  case_id:        {run.case_id}")
    print(f"  selection:      {run.selection.status} → {run.selection.selected_policy_id} ({run.selection.branch})")
    if run.adjudication:
        v_counts = {"met": 0, "not_met": 0, "unclear": 0, "not_documented": 0}
        for v in run.adjudication.leaf_verdicts.values():
            v_counts[v.verdict] = v_counts.get(v.verdict, 0) + 1
        print(f"  leaf verdicts:  met={v_counts['met']}  not_met={v_counts['not_met']}  unclear={v_counts['unclear']}  not_documented={v_counts['not_documented']}")
        print(f"  pipeline cost:  ${run.adjudication.total_usage.cost_usd:.4f}")
        print(f"  cache_read:     {run.adjudication.total_usage.cache_read_tokens:,} tokens")
    print(f"  outcome:        {run.determination.outcome if run.determination else '?'}")
    if run.reviewer:
        print(f"  missing_info:   {len(run.reviewer.output.missing_info)} requests")
    print(f"\nCase persisted to SQLite. View in the React UI at http://localhost:5173")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed a case via the doctor flow: PDF → extractor → bundle → pipeline → persist.",
    )
    parser.add_argument("--pdf", default=str(DEFAULT_SMITH_PDF),
                        help=f"Path to clinical PDF (default: {DEFAULT_SMITH_PDF.relative_to(PROJECT_ROOT)})")
    parser.add_argument("--case-id", default="smith-001", help="Case ID for persistence")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    if not settings.anthropic_api_key:
        raise SystemExit(
            "ANTHROPIC_API_KEY not set.\n"
            "  cp .env.example .env  and paste your key, OR\n"
            "  export ANTHROPIC_API_KEY=sk-..."
        )

    asyncio.run(_seed(pdf_path, args.case_id))


if __name__ == "__main__":
    main()
