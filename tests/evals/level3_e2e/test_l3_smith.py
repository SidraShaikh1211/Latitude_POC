"""Level 3 — End-to-end determination eval (Smith keystone).

Honest path: reads David_Smith_Clinical.pdf from disk, runs the metadata
extractor (Claude) → assembles the Bundle from extracted metadata → runs
the full payer pipeline. No fixture pre-baked with hardcoded CPT/ICD-10.

Expected: pend with missing-info requests mentioning PT documentation or
imaging correlation.

Each L3 case calls the LLM twice (metadata extraction + intake + ~20
adjudicator agents + reviewer). Cost ~$1.50-2 per run. Gated behind
RUN_LLM_EVALS=1.
"""

import os

import pytest

from app.extraction.metadata import extract_submission_metadata
from app.extraction.pdf import extract_pdf
from app.orchestrator import evaluate_pa_case
from app.pas.bundle_constructor import build_pas_bundle
from app.settings import PROJECT_ROOT


pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_EVALS") != "1",
    reason="Set RUN_LLM_EVALS=1 to run end-to-end evals (each case ~$1.5-2 in API).",
)


SMITH_PDF = PROJECT_ROOT / "clinical_pdfs" / "David_Smith_Clinical.pdf"


async def test_E01_smith_pdf_routes_to_esi_policy_and_pends():
    """E01 — Smith keystone via the real PDF→extractor→bundle→pipeline path.
    No pre-baked fixture. Verifies the metadata extractor correctly infers
    everything from the PDF and routes to the ESI policy."""
    assert SMITH_PDF.exists(), f"Smith PDF missing at {SMITH_PDF}"

    pdf_bytes = SMITH_PDF.read_bytes()
    doc = extract_pdf(SMITH_PDF, document_id="L3-smith-doc")

    # Stage 1: extractor pulls submission metadata from the PDF
    meta_result = await extract_submission_metadata(
        doc, pdf_bytes=pdf_bytes, pdf_filename=SMITH_PDF.name,
    )
    # Sanity checks on what the extractor pulled
    assert meta_result.submission.cpt_code.startswith("62") or \
           meta_result.submission.cpt_code.startswith("64"), \
           f"Expected an ESI CPT code; got {meta_result.submission.cpt_code}"
    primary_icds = {c.code for c in meta_result.submission.icd10_codes if c.kind == "primary"}
    # Smith's primary indication is lumbar radiculopathy; M54.* or M51.* are acceptable
    assert any(c.startswith(("M54.", "M51.", "G54.")) for c in primary_icds), \
        f"Expected a lumbar radicular primary diagnosis; got {primary_icds}"

    # Stage 2: build the Bundle from extracted metadata
    bundle, _ = build_pas_bundle(meta_result.submission, case_id="L3-E01-smith")

    # Stage 3: run the full payer pipeline
    run = await evaluate_pa_case(
        bundle, run_intake_on_documents=True, case_id="L3-E01-smith",
    )
    assert run.selection.status == "ok", f"Smith should match a policy; got {run.selection.status}"
    assert run.selection.selected_policy_id == "molina-mcp-032"
    assert run.determination is not None
    assert run.determination.outcome == "pend", (
        f"Expected Smith → pend; got {run.determination.outcome}. "
        f"Rationale: {run.determination.rationale}"
    )

    # The reviewer should produce missing-info requests; at least one should
    # reference PT or imaging (Smith's documented gap)
    assert run.reviewer is not None
    out = run.reviewer.output
    assert len(out.missing_info) >= 1, "Expected at least one missing-info request"
    text = " ".join(mi.request.lower() for mi in out.missing_info)
    assert any(kw in text for kw in ("pt", "physical therapy", "imaging", "mri", "ct")), (
        f"Expected info request mentioning PT or imaging; got: {[mi.request for mi in out.missing_info]}"
    )

    print(f"\n✓ E01 Smith via real extractor: outcome={run.determination.outcome}")
    print(f"  Extracted CPT: {meta_result.submission.cpt_code}")
    print(f"  Primary ICD-10: {primary_icds}")
    print(f"  Missing info ({len(out.missing_info)}):")
    for mi in out.missing_info:
        print(f"    [{mi.id}] {mi.request[:100]}...")
