# PA Prototype — Prior Authorization Prototype

A payer-side prior-authorization decision-support prototype built for the PA Prototype senior-engineer coding assessment. A doctor uploads a single clinical PDF; the system extracts all needed structured fields (CPT, ICD-10, patient demographics, insurance) from the document, assembles a Da Vinci PAS Claim Bundle, routes to the matching policy out of several loaded policies, adjudicates each criterion against the patient's record, and returns a determination (approve / pend / deny) with a clinician-readable narrative and actionable missing-information requests.

**Two keystone cases, one pipeline:**
- **David Smith** (pain management, NY) — submits a chronic low-back-pain fax bundle requesting lumbar interlaminar ESI. Routes to the Molina ESI policy → **pend** with PT-documentation gaps.
- **Catherine Welsh** (gyn, OR) — submits an EMR chart export requesting total laparoscopic hysterectomy for adenomyosis. Routes to the Molina hysterectomy policy → **pend** with hormonal-therapy/imaging documentation gaps.

Both cases enter the system the **same way** — a doctor uploads the PDF, nothing else. The metadata extractor reads the chart and infers everything (including CPT codes from clinical context: "Will proceed with TLH, BS, cysto" → CPT 58571). No hardcoded patient data in the pipeline.

---

## How this hits the four assessment requirements

| # | Brief requirement | How it's implemented |
|---|---|---|
| 1 | **Ingestion of synthetic / de-identified clinical notes** | `app/extraction/pdf.py` (PyMuPDF text + offsets) handles two real document formats: fax bundles (Smith — 23 pages with fax headers, smart-quote artifacts) and EMR exports (Welsh — 15 pages of structured Epic-style encounters). Real-world PDF artifacts (zero-width spaces, smart quotes, inline ICD-10 codes like `ADENOMYOSIS (N80.03)`) handled in `app/llm/citation_verify.py`. |
| 2 | **AI-Powered FHIR Structuring (MCP-aligned)** | Two-stage LLM extraction. (a) `app/extraction/metadata.py` runs **before** Bundle assembly — Claude structured-output infers `SubmissionData` (CPT, ICD-10, patient, coverage) from the PDF, including CPT inference from clinical context when no code is written. (b) `app/extraction/intake.py` runs **after** Bundle parsing — Claude structured-output turns the same PDF into Patient / Conditions / Observations / Medications / Procedures / Allergies / DiagnosticReports with verbatim citations. Every citation is substring-verified against the source PDF (`app/llm/citation_verify.py`); resources with failed citations are dropped. The MCP server (`app/mcp_server/server.py`) exposes the same backend over stdio. |
| 3 | **Document Comparison for Coverage (A2A simulation)** | Two policies loaded from `policies/*.json` (ESI + hysterectomy) — `app/policy/registry.py` substring-verifies all 61 quotes at startup (fail-loud on miss). `app/policy/selector.py` is the deterministic policy selector — filters by CPT, ICD-10 glob, payer, LOB, state, age, care-setting, then resolves ties by specificity score. `app/policy/adjudicator.py` is the per-criterion Claude agent (5-tool surface, parallel via `asyncio.gather`, prompt-cached). The A2A endpoint `POST /fhir/Claim/$submit` accepts a Bundle and returns a Da Vinci PAS `ClaimResponse` Bundle. A doctor-facing endpoint `POST /v1/doctor/submit` accepts a raw PDF and does the same. |
| 4 | **Prototype: citations + criteria + insight + production roadmap** | React UI (`frontend-react/` — Vite + TypeScript + Tailwind + shadcn/ui) with two roles. **Doctor workspace** — drop a PDF, watch live status (📋 Reading PDF → 📨 Payer received → 🔍 Analysis in progress → outcome), then see the brief Reviewer narrative + missing-info requests + extracted-metadata transparency panel + outbound Bundle. **Payer inbox + case detail** — three-panel workspace with structured FHIR + criteria tree (verdict badges, expandable per-criterion: policy quote + patient evidence quote + reasoning) + determination. Production-evolution section below. |

---

## The 15-step clinical review workflow

The brief includes a 15-step clinical-review checklist (the *Clinical review steps* PDF from the assessment package; `clinical_pdfs/` is gitignored — see "Demo PDFs" below). Every step maps to a concrete component:

| Step | Component |
|---|---|
| 1. Confirm the request | `app/extraction/metadata.py::extract_submission_metadata` reads the PDF; `app/pas/bundle_parser.py::parse_pas_bundle` extracts `CaseContext` from the assembled Bundle |
| 2. Identify guideline/policy | `app/policy/selector.py::select_policy` (deterministic filter chain across all loaded policies) |
| 3. Classify request type (initial / repeat) | `app/policy/selector.py::_determine_branch` (reads prior `Procedure` resources from the Bundle) |
| 4. Review clinical documentation | `app/extraction/intake.py::run_intake` (Claude structured-output → FHIR with citations) |
| 5. Validate diagnosis + indication | Adjudicator on diagnosis-supported leaves (uses `lookup_term_class` tool: `M54.16 ∈ lumbar_radiculopathy`, `N80.03 ∈ adenomyosis`) |
| 6. Severity + functional impact | Adjudicator on severity leaves (NRS thresholds, pain-duration temporal checks, QoL impact narrative) |
| 7. Prior treatments / conservative therapy | Adjudicator on conservative-therapy subtrees (uses `lookup_term_class` for NSAID family, hormonal-therapy umbrella, GnRH analogs, IUDs) |
| 8. Objective evidence | Adjudicator hard constraint in `skills/pa-adjudicator/SKILL.md`: `met` verdict requires Observation / DiagnosticReport / quoted exam finding |
| 9. Frequency / dosage / setting | Adjudicator on frequency leaves (uses `check_temporal_constraint` against prior Procedures) |
| 10. Exclusions / contraindications | Per-policy `exclusions` arrays (ESI has 10 named exclusions; gyn policy has implicit gating via ONE_OF) |
| 11. Compare evidence vs criteria | `app/determination/rollup.py` (four-valued logic, all 4 operators) |
| 12. Identify missing information | `app/determination/reviewer.py` (uses `draft_clinician_question` tool to refine into single-response requests) |
| 13. Make/recommend determination | `app/determination/decider.py` (exclusion short-circuit + outcome mapping + escalation routing) |
| 14. Document rationale | Reviewer narrative (references policy by section + patient evidence by document + date) |
| 15. Communicate outcome | `app/pas/bundle_builder.py` → `ClaimResponse` Bundle with `processNote` per criterion + `disposition` carrying the narrative |

---

## Architecture

```
                       Doctor uploads a PDF (no form fields)
                                  │
                                  ▼
POST /v1/doctor/submit   (or POST /fhir/Claim/$submit for an already-assembled Bundle)
                                  │
                                  ▼
       ┌──────────────────────────────────────────────────────────┐
       │  metadata extractor  (Claude structured-output)          │
       │    reads PDF text + infers SubmissionData                │
       │    • Patient: name, DOB, gender, state (from clinic loc) │
       │    • Coverage: payer, member ID, LOB, plan               │
       │    • Service: CPT (inferred from clinical context),      │
       │               DOS, ICD-10 codes (primary + secondary),   │
       │               body site                                  │
       └──────────────────────────────────────────────────────────┘
                                  │
                                  ▼
                       bundle_constructor builds
                       Da Vinci PAS Claim Bundle
                                  │
                                  ▼
       bundle_parser  →  CaseContext  +  FactCollection (current + prior facts)
                                  │
                                  ▼  (if Bundle has DocumentReference+Binary PDFs)
       intake (Claude structured-output)  →  ExtractedFacts with citations
                                  │
                                  ▼
       policy selector (deterministic)
         │  filter: payer → effective date → CPT → ICD-10 glob →
         │           LOB → state → age → care setting → request category
         │  rank: specificity score (narrower CPT scope dominates)
         │  branch: prior Procedure in scope → repeat else initial
         ▼
       adjudicator (Claude agent, 5 tools, parallel per leaf, prompt-cached)
         │  tools: search_facts_by_type · get_document_excerpt ·
         │          check_temporal_constraint · lookup_term_class ·
         │          request_human_review
         │  output: CriterionVerdict {met/not_met/unclear/not_documented,
         │           confidence, patient_evidence[], reasoning, missing_info[]}
         ▼
       rollup (deterministic)
         │  ALL / ONE_OF / NOT / AT_LEAST_K  →  root verdict
         ▼
       decider
         │  exclusion short-circuit  →  outcome (approve/deny/pend/needs_human_review)
         ▼
       reviewer (Claude agent, 5 tools)
         │  tools: get_policy_section · get_patient_facts · get_document_excerpt ·
         │          draft_clinician_question · flag_for_human_review
         │  output: narrative + refined missing-info + escalation flag
         ▼
       bundle_builder  →  Da Vinci PAS ClaimResponse Bundle
                                  │
                                  ▼
                  Returned to doctor's UI + persisted to SQLite
```

**External surfaces, one backend:**
- REST API (FastAPI) — `POST /fhir/Claim/$submit` (A2A entry, accepts a Bundle), `POST /v1/doctor/submit` (PDF-only doctor entry), `/v1/cases/*` and `/v1/policies/*` convenience endpoints
- MCP server — 6 tools, 3 prompts, 2 URI schemes. Mounted at `/mcp` on the FastAPI process (Streamable HTTP); also runnable as a stdio process via `python -m app.mcp_server.server` for Claude Desktop
- A2A Agent Card at `/.well-known/agent.json`
- React UI (Vite + TypeScript + Tailwind + shadcn/ui) with role picker → Doctor Workspace or Payer Inbox + Detail

---

## Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | FastAPI + Pydantic + uvicorn | Pydantic integrates with FHIR shapes; async-native for parallel LLM I/O; OpenAPI free at `/docs`; native multipart for PDF uploads |
| FHIR | `fhir.resources` 8.2 (R4B) | R4 shape validation without HAPI overhead |
| LLM | `anthropic` SDK, `claude-sonnet-4-6` | Cost/capability balance; prompt caching for ~80% reduction on cacheable portions; tool-use for structured-output coercion |
| MCP | official `mcp` Python package | Streamable HTTP transport mounted at `/mcp` on the FastAPI process; same `Server` instance also reachable as stdio via `python -m app.mcp_server.server` |
| PDF | `pymupdf` | Citation needs page coordinates; no OCR fallback (both test PDFs text-extract cleanly) |
| DB | SQLite + SQLAlchemy + aiosqlite (WAL mode) | Sufficient for prototype; Postgres is a one-line swap |
| UI | React 18 + Vite + TypeScript + Tailwind + shadcn/ui (Radix primitives) | TanStack Query for typed fetching + per-case polling that auto-stops on terminal stage; text-excerpt citations rather than PDF iframes |
| Logging | `structlog` (JSON) | Distributed-trace-friendly out of the box |
| Tests | `pytest` + `pytest-asyncio` | 149 unit + integration + L0/L1 eval tests; 16 LLM-gated for L2/L3 |

---

## What's deliberately out of scope (documented choices)

These were considered and explicitly cut to fit the prototype scope. Each is one PR away from being added:

- **HAPI FHIR server** — `fhir.resources` is sufficient for R4 shape validation
- **Da Vinci PAS IG profile validation** — base R4 shape only
- **CQL execution** — the criteria tree + per-leaf LLM adjudication is equivalent
- **X12 278** — CMS-0057-F enforcement discretion makes FHIR-only legitimate
- **CDS Hooks / CRD** — upstream of the demo
- **US Core profile validation** — base R4 sufficient
- **OAuth / SMART on FHIR** — mock with API keys in `.env`
- **OCR fallback** — both test PDFs text-extract cleanly
- **Deployment (Fly.io / Cloud Run)** — local-only by design; deploy is one Dockerfile away
- **Langfuse / OpenTelemetry** — structlog JSON logs + `Usage` accounting are enough for this scope
- **Pre-baked fixtures** — every case enters via the live PDF→extractor→bundle path; there are no hardcoded patient bundles

---

## Project layout

```
Latitude_POC/
├── README.md                              ← you are here
├── requirements.txt
├── Makefile                               install / seed / run / ui / mcp / test / eval
├── .env.example                           ANTHROPIC_API_KEY etc.
├── app/
│   ├── main.py                            FastAPI entry; mounts /v1/* + /fhir/* + /.well-known/agent.json
│   ├── settings.py                        pydantic-settings
│   ├── orchestrator.py                    end-to-end pipeline (shared by REST + MCP + seed)
│   ├── api/
│   │   ├── doctor.py                      POST /v1/doctor/submit  (PDF-only doctor entry; metadata extracted at runtime)
│   │   ├── fhir_pas.py                    POST /fhir/Claim/$submit  (A2A entry; pre-assembled Bundle)
│   │   ├── cases.py                       GET /v1/cases · GET /v1/cases/{id}  (incl. processing_stage + extracted_facts)
│   │   ├── policies.py                    GET /v1/policies · GET /v1/policies/{id}
│   │   └── a2a.py                         GET /.well-known/agent.json
│   ├── mcp_server/server.py               MCP stdio server (6 tools, 3 prompts, 2 URI schemes)
│   ├── extraction/
│   │   ├── pdf.py                         PyMuPDF wrapper (deterministic, no LLM)
│   │   ├── metadata.py                    Claude → SubmissionData (CPT/ICD-10 inference)  ← runs BEFORE bundle assembly
│   │   └── intake.py                      Claude → FHIR resources w/ citations  ← runs AFTER bundle parse
│   ├── policy/
│   │   ├── registry.py                    loads policies/*.json + substring-verifies every quote
│   │   ├── selector.py                    deterministic match (payer, CPT, ICD-10, state, age, ...)
│   │   ├── tree.py                        ALL / ONE_OF / NOT / AT_LEAST_K dataclasses
│   │   ├── adjudicator.py                 per-criterion Claude agent (5 tools, parallel)
│   │   ├── tools.py                       adjudicator tool implementations
│   │   └── term_class.py                  synonym/class dictionary (NSAIDs, hormonal therapy, ICD-10 groupings)
│   ├── determination/
│   │   ├── rollup.py                      deterministic four-valued logic
│   │   ├── decider.py                     exclusion short-circuit → outcome
│   │   └── reviewer.py                    narrative + missing-info Claude agent
│   ├── pas/
│   │   ├── bundle_parser.py               inbound Bundle → CaseContext + facts
│   │   ├── bundle_constructor.py          SubmissionData → outbound Bundle (used by doctor flow)
│   │   └── bundle_builder.py              determination → ClaimResponse Bundle
│   ├── models/                            SQLAlchemy ORM (Case, Document, Policy, Verdict, AuditLog)
│   ├── db/                                async engine (WAL), repositories
│   └── llm/                               Anthropic client wrapper (caching, agent loop), citation_verify
├── skills/
│   ├── pa-metadata-extractor/SKILL.md     system prompt for the PDF→SubmissionData extractor
│   ├── pa-intake/SKILL.md                 system prompt for the FHIR-extraction intake agent
│   ├── pa-adjudicator/SKILL.md            system prompt for the per-criterion adjudicator
│   └── pa-reviewer/SKILL.md               system prompt for the reviewer
├── policies/
│   ├── molina-mcp-032.json                ESI policy — 22 leaves + 10 exclusions (44 verified citations)
│   ├── molina-gyn-hyst-039.json           Hysterectomy policy — 15 leaves + ONE_OF(SectionA, SectionB), 17 verified citations
│   └── sources/                           canonical PDF sources (citation verification targets)
├── frontend-react/                       React UI (Vite + TS + Tailwind + shadcn/ui)
│   ├── src/
│   │   ├── main.tsx                      router + TanStack Query provider
│   │   ├── lib/api.ts                    typed fetch wrappers
│   │   ├── types/api.ts                  Verdict, Outcome, CaseDetail, PolicyDetail...
│   │   ├── components/                   AppLayout, FhirPanel, CriteriaTree, OutcomeBadge...
│   │   └── pages/
│   │       ├── Landing.tsx               role picker + loaded policies
│   │       ├── DoctorWorkspace.tsx       PDF upload + per-case polling every 2s
│   │       ├── PayerInbox.tsx            case list with status badges
│   │       └── PayerCaseDetail.tsx       three-panel workspace (FHIR + criteria tree + determination)
│   └── package.json                      vite, react-router, @tanstack/react-query, radix
├── tests/
│   ├── unit/                              ~115 unit tests (synthetic Bundles, no fixture dependency)
│   ├── integration/                       end-to-end snapshot tests
│   ├── evals/
│   │   ├── level0_citation/               every quote substring-verifies (CI gate)
│   │   ├── level1_selector/               15 selector cases (auto-generated)
│   │   ├── level2_criteria/               criterion eval (synonyms, temporal, exclusions; LLM-gated)
│   │   └── level3_e2e/                    Smith PDF → extractor → pipeline (LLM-gated; honest path)
│   └── fixtures/                          (no pre-baked patient bundles — fixtures are constructed inline by tests)
└── scripts/
    ├── seed_smith_case.py                 ingest any PDF via the doctor flow (default: Smith) → persist
    └── run_evals.py                       run all 4 eval levels with metrics
```

---

## Running it

> ⚠️ **Clinical PDFs are not in the repo.** The `clinical_pdfs/` folder is
> gitignored (assessment materials aren't redistributable). Before running
> `make seed` or the doctor-UI demo, you need to obtain or supply your own
> clinical PDFs and place them locally — see "Demo PDFs" below.

```bash
# One-time setup
cp .env.example .env                            # paste your ANTHROPIC_API_KEY
.venv/bin/pip install -r requirements.txt       # or `make install`

# Seed a case via the doctor flow (~3-4 min, ~$1-2 in API).
# Default path is clinical_pdfs/David_Smith_Clinical.pdf — pass --pdf to override.
make seed
# OR: any other PDF:
# .venv/bin/python -m scripts.seed_smith_case --pdf path/to/some.pdf --case-id custom-001

# Terminal A — backend (FastAPI on :8000, /docs, MCP at /mcp)
make run

# Terminal B — React UI (one-time install, then dev server)
make ui-install                                 # first time only
make ui                                         # Vite, http://localhost:5173

# Run tests
make test                                       # 149 tests + 16 LLM-gated skipped
```

### React UI (Vite + TypeScript + Tailwind + shadcn/ui)

`frontend-react/` is the UI. It talks to the FastAPI backend over HTTP;
FastAPI has CORS enabled for the Vite dev origin (`:5173`) and preview
(`:4173`). Four screens: landing, doctor workspace with live polling,
payer inbox, payer case detail.

```bash
make ui-install      # cd frontend-react && npm install (one-time)
make ui              # vite dev server on :5173 (HMR)
make ui-build        # production build to frontend-react/dist/
```

Override the API base at build time or in `frontend-react/.env`:

```bash
VITE_API_BASE=http://127.0.0.1:8000 npm run dev
```

### Demo PDFs

The repo doesn't ship the patient or source-policy PDFs (they came with the
Latitude assessment package and aren't republished). To run the full demo:

1. Place your `David_Smith_Clinical.pdf` and `Catherine Welsh MR.pdf` (or
   any other clinical PDFs you want to test) inside `clinical_pdfs/`.
2. The two **policy** JSONs (`molina-mcp-032.json`, `molina-gyn-hyst-039.json`)
   are tracked, and their **source PDFs** are in `policies/sources/` (also
   tracked) — so policy loading + citation verification works on a fresh clone.
3. Without `clinical_pdfs/David_Smith_Clinical.pdf`, `make seed` errors
   with `PDF not found`. Either supply one or use `--pdf <some-other.pdf>`.
4. The doctor UI's file uploader works with **any** clinical PDF — drop in
   your own and the metadata extractor will infer the fields.

### Demo flow A — Doctor (the realistic flow)

1. http://localhost:8501 → **🩺 Doctor** card → Doctor Workspace
2. **Drop any clinical PDF** into the uploader (Smith / Welsh / your own). No form fields.
3. Click **📤 Submit to Payer**
4. Watch the spinner: *"Reading your PDF and extracting metadata..."* (~10-15s)
5. The submission response shows you what the extractor pulled — patient name, DOB, CPT (with the reasoning for any inferred values), ICD-10 codes. You can verify it before the slow pipeline takes over.
6. The status card updates live every 2 seconds:
   - 📋 Reading your PDF → 📨 Payer received request → 🔍 Request analysis in progress → ✅ outcome
7. When complete, you see:
   - **Outcome badge**: 🟢 APPROVED / 🟡 PENDED / 🔴 DENIED / 🟣 NEEDS HUMAN REVIEW
   - **Reviewer narrative** (1-2 paragraphs, brief, references the policy by section)
   - **Missing-information requests** if pended (each answerable in a single provider response)
   - **What we extracted from the PDF** expander
   - **Outbound FHIR Bundle** expander (the wire format an EHR would receive)
   - **📋 View full payer detail** link → jumps to the payer's case-detail page

### Demo flow B — Payer (criteria-tree drill-down)

1. http://localhost:8501 → **🏥 Payer** card → Inbox
2. Click any case → three-panel workspace
3. **Left:** structured FHIR (intake-extracted) with `[p.N]` expanders showing the cited source text
4. **Right:** criteria tree with verdict badges — click any leaf for policy citation + adjudicator reasoning + patient evidence quoted verbatim
5. **Below:** outcome + Reviewer narrative + missing-info + raw outbound PAS Bundle

### Demo flow C — Pure REST / API-first

```bash
# PDF-only doctor submission (supply your own PDF path)
curl -X POST http://localhost:8000/v1/doctor/submit \
  -F "pdf=@path/to/clinical_document.pdf"
# Returns: {case_id, processing_stage: "received", extracted_metadata, bundle_preview}
# Then poll: curl http://localhost:8000/v1/cases/<case_id>

# A2A entry (pre-assembled Bundle — what a real EHR would POST)
curl -X POST http://localhost:8000/fhir/Claim/\$submit \
     -H "Content-Type: application/json" \
     -d @my_constructed_bundle.json
# Returns: ClaimResponse Bundle (synchronous)
```

### Demo flow D — MCP (Claude Desktop)

```json
{
  "mcpServers": {
    "latitude-pa": {
      "command": "/absolute/path/to/Latitude_POC/.venv/bin/python",
      "args": ["-m", "app.mcp_server.server"],
      "cwd": "/absolute/path/to/Latitude_POC"
    }
  }
}
```

Then ask Claude: *"Evaluate the latest PA case and show me the missing-info requests."* Claude will call `list_cases` → `get_case` and surface the determination.

---

## The two keystone cases

### Case 1 — David Smith (ESI for back pain, NY → routes to molina-mcp-032)

50yo male, Molina Medicaid NY. Fax bundle requesting CPT 62323 (lumbar interlaminar ESI). Primary M54.16 (radiculopathy), with M79.18 (myalgia) and M47.816 (spondylosis) also on visit-diagnoses. NRS 9/10 pain, NSAIDs + Tylenol + cyclobenzaprine + gabapentin tried, PT prescribed but eval notes "too painful to start."

**System routes to** `molina-mcp-032` (Molina ESI policy, NY footprint, CPT 62323 covered, ICD-10 M54.* matches `applies_to`).

**Outcome:** `pend`. The naive interpretation might approve (radicular diagnosis, severity high, multiple medications) or deny (myofascial code present, PT not completed). The system identifies:
- Conservative therapy incomplete (PT planned-not-completed)
- Reviewer asks for documentation of either completed PT or imaging-correlation contraindication rationale

### Case 2 — Catherine Welsh (hysterectomy for adenomyosis, OR → routes to molina-gyn-hyst-039)

55yo female, Oregon Medicaid (re-badged Molina for demo). Epic-style chart export. **No CPT written anywhere in the document.** Patient diagnoses include N80.03 (adenomyosis), N94.6 (dysmenorrhea), I26.99 (PE), D68.59 (Protein S deficiency). Tried Mirena IUD (painful intercourse), DMPA (side effects), ibuprofen 800mg TID. US shows "heterogeneous myometrium, suggestive of possible adenomyosis." Plan: "Will proceed with TLH, BS, cysto" buried in Encounter #4 Assessment & Plan.

**Metadata extractor infers** CPT 58571 (total laparoscopic hysterectomy with removal of tubes/ovaries) from the "TLH, BS, cysto" phrasing — citing the page in `extraction_notes`. State inferred as OR from "Womens Health Center of Southern Oregon" and "Medford, OR" lab addresses.

**System routes to** `molina-gyn-hyst-039` (Hysterectomy policy, OR footprint, CPT 58571 in covered list, ICD-10 N80.03 matches `N80.*`).

**Outcome:** `pend` (Section B adenomyosis pathway). Reviewer asks for:
- Duration confirmation of hormonal therapy trial
- Pharmacy refill records confirming continuous NSAID use
- MRI report with junctional zone measurement (the policy accepts either US "suggestive of adenomyosis with hypoechoic myometrium" OR MRI with junctional zone >12mm; the US wording was ambiguous)

**Both cases route correctly and produce honest pend outcomes** based only on what the metadata extractor pulled from their PDFs. No hardcoded patient data anywhere.

---

## Testing

```bash
make test                                       # 149 tests + 16 LLM-gated skipped
.venv/bin/pytest tests/unit/ -v                 # unit tests
.venv/bin/pytest tests/evals/level0_citation/   # citation faithfulness gate
.venv/bin/pytest tests/evals/level1_selector/   # 15 selector cases
RUN_LLM_EVALS=1 make eval                       # full pyramid incl. L2/L3 (LLM cost ~$2-10)
```

**Eval pyramid summary:**

| Level | What | Cases | Pass target |
|---|---|---|---|
| L0 | Citation faithfulness (substring-verify every quote across both policies) | 61 (44 ESI + 17 hyst) | 100% |
| L1 | Policy selector | 15 attribute-cross-product cases | ≥98% |
| L2 | Per-criterion adjudication | ~14 illustrative + extensible | ≥92% on clear cases |
| L3 | End-to-end determination | Smith PDF via real extractor (no fixture) | Smith pends with PT/imaging info requests |

L2 and L3 cases call the Claude API; budget ~$0.10–$2 per case depending on tool-loop iterations.

---

## Production roadmap

What this prototype shows about how the design would evolve toward production use:

1. **FHIR APIs at scale** — `/fhir/Claim/$submit` is the contract. To productionize: add HAPI FHIR persistence (so `Claim` and `ClaimResponse` resources are addressable by ID), wire SMART-on-FHIR / OAuth client credentials, add IG profile validation (Da Vinci PAS, US Core).
2. **A2A endpoints** — `/.well-known/agent.json` advertises capabilities today. Production would add JWT-bearer auth, rate limiting, signed responses (FHIR Verifiable Credentials), and an outbound webhook channel for asynchronous determinations.
3. **MCP orchestration** — the MCP server exposes the backend over Streamable HTTP at `/mcp` and stdio. Production paths: (a) per-payer policy registries federated under one root, (b) MCP prompts that walk a clinician through case-construction, (c) OAuth / bearer-token auth on the HTTP transport for non-local clients.
4. **Adjudicator iteration** — the agent's tool calls and verdicts are persisted in `audit_log` for replay. Production would ingest these into Langfuse / OpenTelemetry, run per-policy CI on every policy JSON edit, and use medical-director feedback to fine-tune the Reviewer.
5. **Policy authoring** — `policies/*.json` is hand-authored today, with substring-grep validation. Production would add: (a) a policy authoring UI for clinical analysts (not engineers), (b) automated re-authoring when source PDFs are republished, (c) versioning + A/B testing across policy versions.
6. **Multi-payer** — currently both policies are tagged as Molina for demo simplicity. The selector already supports multi-payer; production would load each real payer's policy library and route via `Coverage.payor` from the inbound Bundle.
7. **Performance** — End-to-end runs in 100s-230s today (~$1.5-2 per case). With prompt caching properly warmed, the adjudicator iteration budget tuned, and the deterministic pieces gating LLM calls more aggressively, the same case should run in 60-90 seconds at ~$0.30.

---

## Honest limitations

- **Cost per case** is $1.5-2 (above the $0.30 design target). The adjudicator hits max-iterations on hard leaves; with iteration budget tuning and longer cache TTLs it should drop to ~$0.50.
- **Intake citation pass rate** is ~95-98% on real PDFs (PDF artifacts like smart-quotes and zero-width spaces cause occasional drops). Production would add OCR fallback + character-class normalization to push to 100%.
- **AT_LEAST_K operator** is implemented and unit-tested but not currently exercised by either loaded policy (no natural "at least 2 of N" criterion in the source texts). Reserved for future policies.
- **MCP transport** runs both Streamable HTTP (at `/mcp` on the FastAPI process) and stdio (`python -m app.mcp_server.server`). The HTTP transport is stateless — fine for the one-shot tool calls in this prototype; long-lived subscriptions / resumable streams would require flipping to a stateful session manager.
- **Two policies loaded.** Adding more is a JSON-file drop into `policies/`. The selector eval (cases S11, S12) already proves multi-policy disambiguation works.
- **Payer is hardcoded to "molina"** in the metadata extractor default when no insurance info is in the PDF. Production would route based on real `Coverage.payor` from the EHR submission.

---

## License + credits

Built for the PA Prototype senior-engineer coding assessment, May 2026.
- The Molina ESI Clinical Policy MCP-032 (August 2024) is from Molina's public clinical policy library.
- The hysterectomy policy is adapted from Oregon Health Authority Prioritized List Guideline Note 39 (re-badged as a Molina policy for demo simplicity — single insurer with multiple policies).
- Both patient PDFs (David Smith, Catherine Welsh) were provided as part of the assessment materials.
