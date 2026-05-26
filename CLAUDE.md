# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A payer-side prior-authorization decision-support prototype. A doctor uploads a clinical PDF; a metadata extractor infers `SubmissionData` (CPT/ICD-10/patient/coverage) from the document, the system assembles a Da Vinci PAS Claim Bundle, deterministically selects a policy, adjudicates each criterion against the patient's record, and returns approve/pend/deny with a clinician-readable narrative.

See `README.md` for the full architecture diagram, two keystone cases, GCP deployment, and the production roadmap. This file is the operational quick-ref.

## Commands

All Python commands use `.venv/bin/python` (or `.venv/bin/pytest`, `.venv/bin/uvicorn`). The repo expects you to operate inside that venv — see `Makefile` for the canonical invocations.

```bash
make install            # pip install -r requirements.txt into .venv
make run                # uvicorn app.main:app on :8000  (--reload-dir app only)
make ui-install         # one-time: cd frontend-react && npm install
make ui                 # Vite dev server on :5173
make mcp                # MCP server in stdio mode (python -m app.mcp_server.server)
make seed               # Ingest default PDF via the doctor flow (~3-4 min, ~$1-2)
make test               # pytest tests/ -v  (LLM-gated L2/L3 skipped by default)
make eval               # python -m scripts.run_evals  (full eval pyramid)
make clean              # remove caches + local SQLite db
make docker-build       # build production image
make deploy SQL_INSTANCE=$PROJECT:$REGION:$INSTANCE   # Cloud Build → Cloud Run
```

Running a single test or eval level:
```bash
.venv/bin/pytest tests/unit/test_some_file.py::test_name -v
.venv/bin/pytest tests/evals/level0_citation/   # citation faithfulness gate
.venv/bin/pytest tests/evals/level1_selector/   # selector cases
RUN_LLM_EVALS=1 .venv/bin/pytest tests/evals/level2_criteria/   # LLM-gated
```

Seeding any non-default PDF:
```bash
.venv/bin/python -m scripts.seed_smith_case --pdf path/to/file.pdf --case-id custom-001
```

The dev server intentionally uses `--reload-dir app` so edits to `skills/`, `policies/`, the React app, or the SQLite DB do **not** kill in-flight pipelines. Don't broaden the reload scope without thinking about that.

## Pipeline architecture

End-to-end pipeline (`app/orchestrator.py` is the single entry point shared by REST + MCP + seed):

```
PDF
 → app/extraction/metadata.py   Claude → SubmissionData (CPT/ICD-10 inference)   [BEFORE bundle]
 → app/pas/bundle_constructor.py    SubmissionData → outbound PAS Claim Bundle
 → app/pas/bundle_parser.py         Bundle → CaseContext + FactCollection
 → app/extraction/intake.py     Claude → FHIR resources with citations          [AFTER bundle parse]
 → app/policy/selector.py           deterministic filter chain + specificity tie-break
 → app/policy/deterministic_eval.py Python short-circuit for structurally-checkable leaves
 → app/policy/evidence_digest.py    builds the per-case digest placed in the cached system block
 → app/policy/adjudicator.py        per-leaf Claude agent (5 tools, parallel, 120s budget)
 → app/determination/rollup.py      four-valued logic: ALL / ONE_OF / NOT / AT_LEAST_K
 → app/determination/decider.py     exclusion short-circuit → outcome
 → app/determination/reviewer.py    narrative + refined missing-info Claude agent
 → app/pas/bundle_builder.py        determination → ClaimResponse Bundle
```

Two extraction stages — they look similar but are distinct:
- **Metadata** (`extraction/metadata.py`) runs **before** Bundle assembly. Output is `SubmissionData` (a small dataclass of CPT/ICD-10/patient/coverage) used to construct the outbound Bundle. CPT may be *inferred* from clinical context ("Will proceed with TLH, BS, cysto" → CPT 58571) with reasoning kept in `extraction_notes`.
- **Intake** (`extraction/intake.py`) runs **after** Bundle parsing. Output is FHIR resources (Patient/Conditions/Observations/etc.) with verbatim citations into the source PDF.

Every citation produced by intake is substring-verified against the PDF (whitespace-normalized, see `app/llm/citation_verify.py`). Failed citations are dropped *individually*; a resource is dropped only when *zero* citations on it survive (`_verify_and_drop` in `extraction/intake.py`). Don't change this to all-or-nothing without reading the comment there.

## Key invariants

- **No hardcoded patient data.** Both keystone cases (Smith, Welsh) enter via the live PDF→extractor→bundle path. There are no pre-baked patient bundle fixtures — tests construct synthetic Bundles inline. Don't add fixture-style bundles to `tests/fixtures/` for new cases.
- **Policy citations are verified at startup.** `app/policy/registry.py` substring-verifies every quoted span in every loaded policy against its source PDF in `policies/sources/` (fail-loud on miss). Adding/editing a policy means the source PDF must exist locally and the quote text must match exactly.
- **Adjudicator runs leaves in parallel with a 120s per-leaf budget** (`asyncio.wait_for`). A stuck leaf becomes synthetic `unclear`, never drops the batch. The system prompt has a cached case-evidence digest so every parallel leaf reads it from prompt cache (~10% the cost of fresh input). Don't refactor the adjudicator to serialize leaves or to rebuild the digest per leaf.
- **`met` verdicts with no surviving cited evidence auto-downgrade to `unclear`** in the post-pass. This is enforced in `app/policy/adjudicator.py` after `verify_substring`. The hard rule is also in `skills/pa-adjudicator/SKILL.md` (`met` requires Observation / DiagnosticReport / quoted exam finding).
- **Deterministic short-circuit is conservative.** `app/policy/deterministic_eval.py` resolves leaves with `evaluator_kind ∈ {diagnosis_code, numeric_threshold, class_membership, numeric_count}` in Python without an LLM call, and returns `None` on any ambiguity so the LLM still runs. Don't broaden a handler to "guess" — the contract is exact-or-defer.
- **Skills are runtime-loaded prompts.** Files under `skills/<name>/SKILL.md` are the live system prompts for the metadata extractor, intake, adjudicator, and reviewer. They are loaded at runtime — they must be present in the container image. Never add `skills/` to `.dockerignore` ([[feedback_docker_skills_dir]]).
- **A2A self-loopback URLs default to `http://127.0.0.1:8000`.** On Cloud Run, uvicorn binds `$PORT` (8080). `settings.payer_pas_base_url` and `settings.doctor_callback_base_url` must be set as env vars in the deployed service or the doctor→payer handoff fails with `httpx.ConnectError` ([[feedback_a2a_loopback_port]]).

## Where things live

- `app/orchestrator.py` — single pipeline entry point, emits stage events to `app/events.py` for the SSE stream at `GET /v1/cases/{id}/events`.
- `app/api/` — REST surface (`doctor.py`, `fhir_pas.py`, `cases.py`, `policies.py`, `a2a.py`).
- `app/mcp_server/server.py` — MCP server (6 tools, 3 prompts, 2 URI schemes), mounted at `/mcp` on the FastAPI process *and* runnable as stdio.
- `app/llm/` — Anthropic client wrapper (prompt caching, agent loop, `Usage.cache_hit_rate`), `citation_verify.py` (whitespace-normalized substring match).
- `app/db/` — async SQLAlchemy (WAL on SQLite), repositories, `orphan_sweep` (marks stale `processing` rows on restart). The Cloud SQL Postgres path uses asyncpg over a Unix socket — SQLite-specific ALTER TABLE logic in `engine.py` is already guarded.
- `app/models/` — SQLAlchemy ORM (Case, Document, Policy, Verdict, AuditLog).
- `policies/*.json` — policy JSONs (criteria tree + exclusions). Each one references a PDF in `policies/sources/` that must exist for citation verification.
- `skills/<name>/SKILL.md` — live system prompts; edit these, not Python string literals, to change LLM behavior.
- `frontend-react/` — Vite + TS + Tailwind + shadcn/ui. TanStack Query + an SSE subscription that auto-closes on terminal stage. CORS in FastAPI is open for `:5173` and `:4173`.
- `tests/evals/level{0,1,2,3,4}_*` — eval pyramid. L0/L1/L4 always run; L2/L3 need `RUN_LLM_EVALS=1` and cost API budget.

## Conventions

- Logs are structlog JSON.
- Models default to `claude-sonnet-4-6` (configurable via `ANTHROPIC_MODEL`).
- Database default is `sqlite+aiosqlite:///data/app.db`. Production overrides with `DATABASE_URL` from Secret Manager (`postgresql+asyncpg://...?host=/cloudsql/...`); `pydantic-settings` picks it up automatically.
- Cloud Build uses `$BUILD_ID` for image tags (works for direct `gcloud builds submit`). Don't switch to `$SHORT_SHA` — it's empty outside Git triggers ([[feedback_cloudbuild_short_sha]]).
- Uploaded PDFs in `data/pdfs/` are ephemeral on Cloud Run — fine for the demo, but not durable ([[project_ephemeral_pdfs]]).
