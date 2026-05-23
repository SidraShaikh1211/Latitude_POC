# PA Prototype — Prior Authorization Prototype

A payer-side prior-authorization decision-support prototype built for the PA Prototype senior-engineer coding assessment. Accepts Da Vinci PAS Claim Bundles, extracts citation-grounded FHIR resources via Claude, evaluates against the Molina ESI policy (~30 criteria), runs per-criterion adjudication with a 5-tool agent, and returns a Da Vinci PAS ClaimResponse Bundle with a clinician-readable narrative and refined missing-info requests.

**Keystone case:** the David Smith ESI submission (CPT 62323 on Molina Medicaid NY) produces `pend` with two actionable info requests, exactly as a well-designed system should — a naive yes/no system gets this wrong.

---

## How this hits the four assessment requirements

| # | Brief requirement | How it's implemented |
|---|---|---|
| 1 | Ingestion of synthetic / de-identified clinical notes | `app/extraction/pdf.py` (PyMuPDF text + offsets) + `scripts/seed_smith_case.py` (builds a Da Vinci PAS Bundle wrapping the David Smith PDF as DocumentReference + Binary). Real-world fax artifacts (zero-width spaces, smart quotes) handled in `app/llm/citation_verify.py`. |
| 2 | **AI-Powered FHIR Structuring (MCP-aligned)** | `app/extraction/intake.py` uses Claude structured-output (via tool-use coercion) to turn unstructured PDFs into Patient / Conditions / Observations / MedicationRequests / Procedures / AllergyIntolerances / DiagnosticReports, each with verbatim source citations. Every citation is substring-verified against the source PDF (`app/llm/citation_verify.py`); resources with failed citations are dropped. MCP server (`app/mcp_server/server.py`) exposes `extract_clinical_facts`, `evaluate_prior_auth`, and resource URIs (`policy://`, `case://`) so an MCP client (Claude Desktop) can drive the same backend. |
| 3 | **Document Comparison for Coverage (A2A simulation)** | `policies/molina-mcp-032.json` is the criteria tree (22 leaves + 10 exclusions) authored against the Molina ESI MCP-032 policy. `app/policy/registry.py` loads + substring-verifies every quote at startup (fail-loud on miss). `app/policy/selector.py` is the deterministic policy selector (CPT / ICD-10 glob / payer / LOB / state / age / care-setting filter + specificity disambiguation + branch determination from prior Procedure resources in the Bundle). `app/policy/adjudicator.py` is the per-criterion Claude agent (5-tool surface, parallel via `asyncio.gather`, prompt-cached). `POST /fhir/Claim/$submit` is the A2A endpoint that closes the loop: in goes a Da Vinci PAS Claim Bundle, out comes a PAS ClaimResponse Bundle. |
| 4 | Prototype: citations + criteria + insight + production roadmap | Streamlit UI (`frontend/`) shows structured FHIR with `[p.N]` text-excerpt expanders, criteria tree with verdict badges (✅ / ❌ / 🟡 / 📭), determination outcome + Reviewer narrative + missing-info requests, and the raw outbound PAS Bundle. The Reviewer agent (`app/determination/reviewer.py`, also 5 tools) produces clinician-readable narratives that quote the policy by section + patient evidence by document + date. Production evolution is in the *Roadmap* section below. |

---

## The 15-step clinical review workflow

The brief includes a 15-step clinical-review checklist (`clinical_pdfs/Clinical review steps.pdf`). Every step maps to a concrete component:

| Step | Component |
|---|---|
| 1. Confirm the request | `app/pas/bundle_parser.py::parse_pas_bundle` → `CaseContext` (CPT, ICD-10, payer, LOB, state, age, service date, urgency, care setting) |
| 2. Identify guideline/policy | `app/policy/selector.py::select_policy` (deterministic filter chain) |
| 3. Classify request type (initial / repeat) | `app/policy/selector.py::_determine_branch` (reads prior `Procedure` resources from the Bundle) |
| 4. Review clinical documentation | `app/extraction/intake.py::run_intake` (Claude structured-output → FHIR with citations) |
| 5. Validate diagnosis + indication | Adjudicator on `indication.initial_injection.diagnosis_supported.*` leaves (uses `lookup_term_class` tool: `M54.16 ∈ lumbar_radiculopathy`) |
| 6. Severity + functional impact | Adjudicator on `severity.nrs_above_4` and `severity.adl_impact` (uses `check_temporal_constraint` for thresholds) |
| 7. Prior treatments / conservative therapy | Adjudicator on `conservative_therapy.failure.{pt.*, activity_modification, drug_therapy}` (uses `lookup_term_class` for NSAID class membership) |
| 8. Objective evidence | Adjudicator hard constraint in `skills/pa-adjudicator/SKILL.md`: `met` verdict requires Observation / DiagnosticReport / quoted exam finding |
| 9. Frequency / dosage / setting | Adjudicator on `frequency_and_location.frequency_within_limits.*` (uses `check_temporal_constraint` against prior Procedures) |
| 10. Exclusions / contraindications | `policies/molina-mcp-032.json` exclusions array (10 entries: myofascial, non-radicular, pregnancy, anticoagulation, infection, etc.) evaluated independently |
| 11. Compare evidence vs criteria | `app/determination/rollup.py` (four-valued logic, all 4 operators) |
| 12. Identify missing information | `app/determination/reviewer.py` (uses `draft_clinician_question` tool to refine into single-response requests) |
| 13. Make/recommend determination | `app/determination/decider.py` (exclusion short-circuit + outcome mapping + escalation routing) |
| 14. Document rationale | Reviewer narrative (references policy by section + patient evidence by document + date) |
| 15. Communicate outcome | `app/pas/bundle_builder.py` → `ClaimResponse` Bundle with `processNote` per criterion + `disposition` carrying the narrative |

---

## Architecture

```
POST /fhir/Claim/$submit  (inbound Da Vinci PAS Bundle)
  │
  ▼
bundle_parser  →  CaseContext  +  FactCollection (current + prior facts)
  │
  ▼ (if Bundle has DocumentReference+Binary PDFs)
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
```

**Two external surfaces, one backend:**
- REST API (FastAPI, "API-first") — `POST /fhir/Claim/$submit` + convenience `/v1/*` endpoints
- MCP server (stdio, Claude Desktop compatible) — 6 tools, 3 prompts, 2 URI schemes
- A2A Agent Card at `/.well-known/agent.json`

---

## Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | FastAPI + Pydantic + uvicorn | Pydantic integrates with FHIR shapes; async-native for parallel LLM I/O; OpenAPI free at `/docs` |
| FHIR | `fhir.resources` 8.2 (R4B) | R4 shape validation without HAPI overhead |
| LLM | `anthropic` SDK, `claude-sonnet-4-6` | Cost/capability balance; prompt caching for ~80% reduction on cacheable portions |
| MCP | official `mcp` Python package | stdio transport mounted as `python -m app.mcp_server.server` |
| PDF | `pymupdf` | Citation needs page coordinates; no OCR fallback for this prototype (Smith PDF is text-extractable, verified Day 1) |
| DB | SQLite + SQLAlchemy + aiosqlite (WAL mode) | Sufficient for prototype; Postgres is a one-line swap for production |
| UI | Streamlit (text-excerpt citations) | Fast iteration; no PDF iframe (deliberately avoided per design notes — fragile Streamlit components risk) |
| Logging | `structlog` (JSON) | Distributed-trace-friendly out of the box |
| Tests | `pytest` + `pytest-asyncio` | 131 tests across unit + integration + 4-level eval pyramid |

---

## What's deliberately out of scope (documented choices)

These were considered and explicitly cut to fit the prototype scope. Each is one PR away from being added:

- **HAPI FHIR server** — `fhir.resources` is sufficient for R4 shape validation; HAPI is needed only for FHIR persistence + IG profile validation in production.
- **Da Vinci PAS IG profile validation** — base R4 shape only. Production would add `fhir-validator` or HAPI's profile validator.
- **CQL execution** — the criteria tree + per-leaf LLM adjudication is equivalent for the prototype timeline.
- **X12 278** — CMS-0057-F enforcement discretion makes FHIR-only legitimate.
- **CDS Hooks / CRD** — upstream of the demo.
- **US Core profile validation** — base R4 sufficient for prototype.
- **OAuth / SMART on FHIR** — mock with API keys in `.env`.
- **OCR fallback** — Smith PDF text-extracts cleanly; not needed for the keystone case.
- **React UI** — Streamlit is faster to iterate and the demo doesn't need SPA polish.
- **Deployment (Fly.io / Cloud Run)** — local-only by design; deploy is one Dockerfile away.
- **Langfuse / OpenTelemetry** — structlog JSON logs + `Usage` accounting are enough for this scope.

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
│   ├── orchestrator.py                    end-to-end pipeline (shared by REST + MCP)
│   ├── api/                               REST endpoints (cases, policies, fhir_pas, a2a)
│   ├── mcp_server/server.py               MCP stdio server (6 tools, 3 prompts, 2 URI schemes)
│   ├── extraction/                        PDF + intake (Claude structured-output)
│   ├── policy/                            registry, selector, tree dataclasses, term_class, adjudicator, tools
│   ├── determination/                     rollup, decider, reviewer
│   ├── pas/                               bundle_parser (inbound), bundle_builder (outbound)
│   ├── models/                            SQLAlchemy ORM (Case, Document, Policy, Verdict, AuditLog)
│   ├── db/                                async engine (WAL), repositories
│   └── llm/                               Anthropic client wrapper (caching, agent loop), citation_verify
├── skills/
│   ├── pa-intake/SKILL.md                 system prompt for the intake agent
│   ├── pa-adjudicator/SKILL.md            system prompt for the per-criterion adjudicator
│   └── pa-reviewer/SKILL.md               system prompt for the reviewer
├── policies/
│   ├── molina-mcp-032.json                22 tree leaves + 10 exclusions (44 verified citations)
│   └── sources/molina-mcp-032.pdf         canonical policy source
├── frontend/
│   ├── app.py                             landing page (policies + health check)
│   └── pages/
│       ├── 01_inbox.py                    case list
│       └── 02_case_detail.py              three-panel workspace
├── tests/
│   ├── unit/                              108 unit tests
│   ├── integration/                       end-to-end snapshot tests
│   ├── evals/
│   │   ├── level0_citation/               every quote substring-verifies (CI gate)
│   │   ├── level1_selector/               15 selector cases
│   │   ├── level2_criteria/               criterion eval (synonyms, temporal, exclusions)
│   │   └── level3_e2e/                    Smith + synthetic cases
│   └── fixtures/
│       ├── smith_claim_bundle.json        the inbound Smith PAS Bundle (3.3 MB)
│       └── ...
└── scripts/
    ├── seed_smith_case.py                 build Smith fixture + run e2e + persist
    └── run_evals.py                       run all 4 eval levels with metrics
```

---

## Running it

```bash
# One-time setup
cp .env.example .env                            # paste your ANTHROPIC_API_KEY
.venv/bin/pip install -r requirements.txt       # or `make install`

# Generate the Smith fixture, run the pipeline, persist to SQLite (~3-4 min, ~$1 in API)
make seed

# Terminal A: start the FastAPI backend (also serves OpenAPI at /docs and MCP info)
make run

# Terminal B: start the Streamlit UI
make ui                                         # open http://localhost:8501

# Run the test suite
make test                                       # unit + integration + L0/L1 evals
```

**Demo flow:**
1. Open http://localhost:8501 → see "API connected" + loaded policy summary.
2. Click **Inbox** → see the Smith case with status badge.
3. Click **Open Case Detail** → three-panel layout:
   - **Left:** structured FHIR (Patient, Conditions, Observations, Medications, Procedures) with `[p.N]` expanders showing the cited source quote.
   - **Right:** criteria tree with verdict badges. Click any leaf to see policy citation, adjudicator reasoning, patient evidence quoted verbatim from the PDF.
   - **Below:** determination outcome (🟡 PENDED), Reviewer narrative, missing-info requests, raw outbound PAS ClaimResponse Bundle.
4. Try the API directly:
   ```bash
   curl -X POST http://localhost:8000/fhir/Claim/\$submit \
        -H "Content-Type: application/json" \
        -d @tests/fixtures/smith_claim_bundle.json
   ```
5. Try the MCP server (from Claude Desktop config):
   ```json
   { "mcpServers": { "latitude-pa": {
       "command": "/Users/.../.venv/bin/python",
       "args": ["-m", "app.mcp_server.server"]
   } } }
   ```
   Then ask Claude: *"Evaluate Smith's PA case and show me the missing-info requests."*

---

## What the Smith case demonstrates

David Smith, 50yo male, Molina Medicaid NY. Requesting CPT 62323 (lumbar interlaminar ESI with imaging guidance), primary diagnosis M54.16, with M79.18 (myalgia) and M47.816 (lumbar spondylosis) also on the visit-diagnoses list. Clinical documentation shows:

- NRS 9/10 pain, chronic LBP with bilateral radiation
- NSAIDs (ibuprofen), Tylenol, cyclobenzaprine all tried
- PT was prescribed (20 visits over 10 weeks) but PT eval notes "too painful to start"

**Expected outcome (and what the system produces):** `pend`. The naive interpretation might approve (radicular diagnosis present, severity high, multiple medications tried) or deny (myofascial code present, PT not completed). A well-designed system identifies the ambiguity:
- Conservative therapy is incomplete: NSAIDs OK, but PT was planned-not-completed, and the contraindication-with-imaging pathway requires explicit documentation that wasn't supplied.
- The myofascial exclusion (M79.18) appears in visit diagnoses but the PA was filed under M54.16. The adjudicator correctly determined the *primary* indication is radicular (not myofascial), so the exclusion doesn't fire — but the Reviewer can still ask for explicit confirmation.

The Reviewer's narrative quotes the case verbatim ("too painful to start") and references the policy by section ("Molina MCP-032, Coverage Policy page 2"). The missing-info requests are answerable in a single provider response each.

---

## Testing

```bash
make test                                       # 131 tests
.venv/bin/pytest tests/unit/ -v                 # 108 unit tests
.venv/bin/pytest tests/evals/level0_citation/   # citation faithfulness gate
.venv/bin/pytest tests/evals/level1_selector/   # 15 selector cases
```

**Eval pyramid summary:**

| Level | What | Cases | Pass target |
|---|---|---|---|
| L0 | Citation faithfulness (substring-verify every quote) | 44 (every quote in every policy) | 100% |
| L1 | Policy selector | 15 attribute-cross-product cases | ≥98% |
| L2 | Per-criterion adjudication | ~8 illustrative + extensible | ≥92% on clear cases |
| L3 | End-to-end determination | Smith + extensible synthetic cases | ≥85% exact match |

L2 and L3 cases call the Claude API; budget ~$0.10–$1 per case depending on tool-loop iterations.

---

## Production roadmap

What this prototype shows about how the design would evolve toward production use:

1. **FHIR APIs at scale** — `/fhir/Claim/$submit` is the contract. To productionize: add HAPI FHIR persistence (so `Claim` and `ClaimResponse` resources are addressable by ID), wire SMART-on-FHIR / OAuth client credentials, add IG profile validation (Da Vinci PAS, US Core).
2. **A2A endpoints** — `/.well-known/agent.json` advertises capabilities today. Production would add JWT-bearer auth, rate limiting, signed responses (FHIR Verifiable Credentials), and an outbound webhook channel for asynchronous determinations on long-running cases.
3. **MCP orchestration** — the MCP server today exposes the backend over stdio. Production paths: (a) Streamable HTTP transport mounted on FastAPI for remote clients, (b) per-payer policy registries via additional MCP servers federated under one root, (c) MCP prompts that walk a clinician through case-construction.
4. **Adjudicator iteration** — the agent's tool calls and verdicts are persisted in `audit_log` for replay. Production would: ingest these into Langfuse / OpenTelemetry for fleet-level evaluation, run per-policy CI on every policy JSON edit, and use thumbs-up/down feedback from medical directors to fine-tune the Reviewer system prompt.
5. **Policy authoring** — `policies/*.json` is hand-authored today, with `pdftotext + grep` validation. Production would add: (a) a policy authoring UI for clinical analysts (not engineers), (b) automated re-authoring when source PDFs are republished, (c) versioning + A/B testing across policy versions.
6. **Performance** — Smith case end-to-end runs in ~4 minutes today, ~$1 in API. With prompt caching properly warmed and the adjudicator iteration budget tuned, the same case should run in ~60–90 seconds at ~$0.20. Production gains come from: longer cache TTLs (Anthropic supports beta long-TTL caching), batching adjudication calls, and running the deterministic pieces first to skip LLM calls when verdicts are unambiguous.

---

## Honest limitations

- **Cost per case** is currently ~$1 for the Smith case (above the $0.30 design target). The adjudicator hit max-iterations on 5 leaves on the first run; with the now-implemented retry guidance + iteration budget bump (5 → 8) + intake JSON-coercion fix, repeat runs should drop to ~$0.30–0.50.
- **Intake citation pass rate** was 97.2% on the first Smith run (2 of 72 citations dropped due to PDF artifacts — smart-quote in `M54.‘1 6`, missing string for myofascial). Production would add OCR fallback + character-class normalization to push this to 100%.
- **AT_LEAST_K operator** is implemented and unit-tested but not exercised by the Molina policy (no natural "at least 2 of 3 evidence types" criterion in the source text). Documented as `reserved` in policy metadata.
- **Streamable HTTP MCP** would be a nice-to-have over the current stdio-only transport. The agent loop architecture doesn't change; only the transport wrapper.
- **One policy** loaded. Adding policies is a JSON-file drop; the selector eval (S11, S12) already proves multi-policy disambiguation works.

---

## License + credits

Built for the PA Prototype senior-engineer coding assessment, May 2026. The Molina ESI Clinical Policy MCP-032 (August 2024 version) is publicly available at Molina's clinical policy library; the David Smith PDF was provided as part of the assessment.
