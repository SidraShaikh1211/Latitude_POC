# PA Prototype — Prior Authorization System
## Build Specification

> **Purpose of this document.** A single self-contained specification for building a prototype prior authorization (PA) system that ingests clinical documents, extracts FHIR-structured patient data with citations, selects the applicable medical policy, adjudicates each criterion via LLM, and returns a Da Vinci PAS–shaped determination. Designed for an engineer (or coding agent) to read once and build from. Combines and supersedes prior design documents.
>
> **Estimated build effort.** 5–7 days of focused work.

---

## Table of Contents

1. Business Context
2. Worked Example: The Smith Case
3. Glossary (essential terms)
4. System Architecture
5. Data Models
6. Component Specifications
7. Project Layout
8. Build Phasing
9. Evaluation Framework
10. Observability & Operations
11. Production Roadmap (Explicit Cuts)
12. Open Configuration Decisions
13. Acceptance Criteria

---

## 1. Business Context

### 1.1 The problem

Prior authorization is the most-disliked process in US healthcare. Roughly one in three physician hours goes to PA documentation. Median time from request to decision is 3–5 business days for standard requests. 17% of initial denials are overturned on appeal — a meaningful share of first-pass denials are wrong. The AMA has measured ~24% of patients abandoning care during PA delays.

On the payer side, the median PA case requires a utilization-management (UM) nurse to spend 25–45 minutes reviewing a fax bundle, hand-extracting facts, looking up the applicable policy, and either deciding or escalating. Per-case fully-loaded cost runs $40–100. At a regional plan processing 50,000 PAs/year, that's $2–5M annually in review labor alone.

### 1.2 The regulatory forcing function

CMS-0057-F, the Interoperability and Prior Authorization Final Rule (effective January 2024), requires impacted payers (Medicare Advantage, Medicaid FFS, Medicaid managed care, CHIP, QHPs on the FFE) to implement a FHIR-based Prior Authorization API by **January 1, 2027**. Standard decisions in 7 days, urgent in 72 hours. Specific denial reasons required. Public PA metrics reporting started March 2026. The mandated FHIR shape is exactly the HL7 Da Vinci PAS Implementation Guide.

This is why every payer is currently rebuilding their PA stack.

### 1.3 What we are building

A prototype payer-side PA system that:

1. Accepts a clinical PDF (or an inbound Da Vinci PAS Bundle on the production wire).
2. Extracts FHIR-structured patient facts with citations back to source spans.
3. Selects the applicable medical policy from a registry.
4. Adjudicates each criterion against the patient's facts, producing a four-valued verdict (`met` / `not_met` / `unclear` / `not_documented`) with cited evidence.
5. Returns a structured determination (approve / deny / pend) as a Da Vinci PAS `ClaimResponse` Bundle.
6. Exposes the backend via MCP so AI clients can drive it programmatically.

### 1.4 Success criteria

| Dimension | Target |
|---|---|
| Policy selection accuracy (unambiguous cases) | ≥98% |
| Safe escalation on ambiguous cases | 100% (no silent auto-pick) |
| Criterion evaluation accuracy (clear cases) | ≥92% |
| Citation faithfulness | 100% (every quote substring-verifiable) |
| End-to-end latency | <30s P95 |
| Per-case LLM cost | <$0.30 |

### 1.5 Why this architecture wins

The system treats the *policy* as a machine-readable artifact (a criteria tree with citations) and the *patient chart* as a citation-bearing FHIR graph. With both sides structured and traceable, the LLM's job becomes narrow: per-criterion judgment over a small evidence window. This gives speed without losing accuracy or defensibility. The same shape generalizes to drug PA, surgical PA, DME PA, and inpatient concurrent review.

---

## 2. Worked Example: The Smith Case

A real (de-identified) PA fax bundle drives the entire build. Every component is verified against this case.

**Patient:** David Smith, DOB 1975-11-02 (50yo male), Member ID KF464W, payer Amidacare/MCD (NY Medicaid).

**Service requested:** CPT 62323 — lumbar interlaminar epidural steroid injection (ESI), L4/5 vs L5/S1, DOS 2026-04-08. Primary diagnosis M54.16 (radiculopathy, lumbar region).

**Clinical evidence in the bundle:**
- Pain Management H&P (2026-02-06): chronic lumbar back pain, 9/10 NRS, bilateral lower extremity radiation, numbness/weakness in BLE. Prior trials: NSAIDs (ibuprofen) +, Tylenol +, topical agents +. Physical therapy: "too painful to start."
- Physical Therapy evaluation (2025-06-25): chronic left LBP with sciatica (M54.42), MMT showing weakness, limited tolerance. Plan: 20 visits over 10 weeks.
- Visit diagnoses include myofascial pain (M79.18), lumbar spondylosis (M47.816), lumbar radicular pain (M54.16).

**Why this case is the right demo:**

The expected outcome against Molina ESI Policy 032 is **PEND with two specific information requests**:

1. *Conservative therapy documentation incomplete.* NSAIDs + Tylenol documented as tried (✓ partial). But PT was "too painful to start" rather than completed 4+ weeks. Policy allows alternate pathway (radicular pain with imaging correlation that precludes PT) but requires explicit documentation of that rationale. **Missing info request:** Provider to document either (a) attempted completion of 4 weeks of PT with outcomes, OR (b) imaging correlation and clinical rationale for why PT is contraindicated.
2. *Exclusion check unclear.* Visit diagnoses include myofascial pain (M79.18), which is an explicit policy exclusion. The PA is submitted under M54.16. **Missing info request:** Confirm primary indication is lumbar radicular pain (M54.16), not myofascial pain syndrome.

A naive yes/no system gets this wrong. The system whose value the demo proves is the one that identifies the gaps and produces actionable pend-for-information.

---

## 3. Glossary (essential terms)

### 3.1 FHIR

HL7's standard for health data exchange. Currently at R4 (production-dominant). Models healthcare as a graph of typed Resources connected by References. Resources we use:

| Resource | Represents |
|---|---|
| `Patient` | The person |
| `Coverage` | Their insurance |
| `Condition` | A diagnosis / problem |
| `Observation` | A measurement or finding |
| `MedicationRequest` | A prescribed drug |
| `Procedure` | A performed procedure |
| `ServiceRequest` | A requested procedure |
| `DocumentReference` | A document (note, fax, PDF) |
| `Claim` | A submission for adjudication (PA or billing); type=preauthorization for PA |
| `ClaimResponse` | The payer's response |
| `Bundle` | A container for multiple resources |

### 3.2 HL7 Da Vinci PAS

Da Vinci is HL7's accelerator project for payer/provider exchange. PAS (Prior Authorization Support) defines the FHIR Bundle shape for submitting PAs and receiving responses. The provider sends a Bundle containing a `Claim` (intent=preauthorization) plus supporting resources. The payer returns a Bundle containing a `ClaimResponse` with `outcome` (queued / complete / partial / error), per-item `adjudication`, `preAuthRef`, and `processNote` entries.

### 3.3 MCP (Model Context Protocol)

Open standard from Anthropic (Nov 2024), donated to Linux Foundation (Dec 2025). JSON-RPC 2.0 over either stdio or Streamable HTTP. Lets LLM clients call defined Tools (executable actions), read Resources (URI-addressable data), and use Prompts (templates).

**MCP's role here:** an external interface for *LLM clients* (Claude Desktop, custom agents) to drive our backend. Not the protocol between our system and a hospital — that's FHIR.

### 3.4 A2A

Ambiguous acronym in this domain:
- **Application-to-Application:** the Da Vinci sense. EHR (App 1) talks to payer (App 2) over standardized FHIR APIs. This is our `/fhir/Claim/$submit` endpoint.
- **Agent2Agent:** Google's protocol (April 2025, donated to Linux Foundation June 2025). JSON-RPC over HTTP with Agent Cards (capability advertisement) and Tasks. Optional stretch for our build.

### 3.5 NCD / LCD / Payer Policy

- **NCD** (National Coverage Determination): CMS-issued national Medicare policy.
- **LCD** (Local Coverage Determination): MAC-issued for a geographic area.
- **Payer Medical Policy:** a commercial or Medicaid managed-care plan's coverage rules. Molina Policy 032 (our worked example) is a Medicaid managed-care policy derived from LCDs + evidence.

### 3.6 The protocol boundaries (mental model)

| Wire | Protocol | Carries |
|---|---|---|
| Provider EHR ↔ Payer | FHIR over HTTPS (Da Vinci PAS) | PA Request Bundle / ClaimResponse Bundle |
| LLM client ↔ Our backend | MCP over Streamable HTTP | Tool calls |
| Inside our backend | Python function calls | Everything internal |

**Slogan:** FHIR for systems, MCP for LLMs, function calls within our process.

---

## 4. System Architecture

### 4.1 Pipeline

```
PROVIDER SIDE                                    PAYER SIDE
─────────────                                    ──────────
Upload PDF                                       Receive PAS Bundle
   │                                                  │
   ▼                                                  ▼
Intake Agent extracts FHIR ──► PAS Bundle ──► Policy Selector
   (with citations)            (FHIR/HTTPS)          │
                                                     ▼
                                                Adjudicator
                                                  (parallel per criterion)
                                                     │
                                                     ▼
                                                Rollup + Exclusions
                                                     │
                                                     ▼
                                                Reviewer Agent
                                                  (narrative + missing-info)
                                                     │
                                                     ▼
                                                ClaimResponse Bundle
                                                     │
                                              ◄──────┘
                                              (FHIR/HTTPS back to provider)
```

For the prototype, provider-side and payer-side run in the same process. The boundary remains architecturally clean — provider extracts and attests; payer selects, adjudicates, reviews, responds.

### 4.2 Agent architecture: three bounded agents + deterministic orchestrator

**Decision:** Three specialized agents joined by a deterministic Python orchestrator. NOT one mega-agent with all tools.

| Agent | Job | Tools | Loop budget |
|---|---|---|---|
| **Intake** | Convert sectioned clinical document into citation-grounded FHIR resources | `extract_patient_demographics`, `extract_coverage`, `extract_conditions`, `extract_observations`, `extract_medications`, `extract_procedures`, `extract_service_request`, `lookup_icd10`, `lookup_cpt` | ~25 LLM calls max |
| **Adjudicator** | Evaluate ONE policy criterion against the relevant subset of patient facts | `search_facts_by_type`, `get_document_excerpt`, `check_temporal_constraint`, `request_human_review` | 5 tool calls max per criterion |
| **Reviewer** | Read full structured case; write narrative; refine missing-info requests; flag edge cases | `get_policy_section`, `get_patient_facts`, `get_document_excerpt`, `draft_clinician_question`, `flag_for_human_review` | 10 tool calls max |

**Rationale for three agents:**
- Bounded tool surfaces (5 tools each → ~98% selection accuracy vs ~80% with 25 tools)
- Parallelism where it matters (criteria evaluated concurrently)
- Deterministic execution path (orchestrator owns flow, not the LLM)
- Per-agent evals (regressions are localized)

**Rationale against alternatives:**
- One mega-agent: tool accuracy degrades, no parallelism, hard to eval
- Two agents (Extract + Decide): Decide has too many responsibilities, can't parallelize criteria
- N agents (one per resource type, one per criterion type): combinatorial explosion
- Pure CQL no LLM: weeks per policy, loses narrative-handling capability

**The Reviewer Agent does NOT override the structural verdict.** The deterministic rollup is the source of truth. The Reviewer annotates and writes prose. The verdict comes from the rules engine; the words come from the LLM.

### 4.3 Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| **Backend framework** | FastAPI (Python) | Pydantic maps cleanly to FHIR shapes; native async for LLM I/O; free OpenAPI docs |
| **Validation** | `fhir.resources` (pydantic-based) | R4 shape validation without HAPI overhead |
| **Storage** | SQLite + flat files (PDFs, policies) | Sufficient for prototype; Postgres for production is one config change |
| **PDF parsing** | PyMuPDF for text + bbox; Tesseract OCR fallback | Citation needs page coordinates; AGPL caveat for production |
| **LLM** | Claude Sonnet 4.5 via Anthropic SDK | Right cost/capability for structured extraction and judgment |
| **MCP** | Official Python `mcp` package | Native SDK; mount on FastAPI process |
| **Demo UI** | Streamlit (Day 1 target); React (Day 6 if time permits) | Optimize for screenshot quality |
| **Deploy** | Fly.io | Single Dockerfile, persistent volume, free tier sufficient |
| **Observability** | structlog + OpenTelemetry + Langfuse | Logs, traces, LLM-specific introspection |

### 4.4 Workflow Mapping — How the Pipeline Maps to the 15-Step Clinical Review

Latitude's PA workflow specification (Clinical_review_steps.pdf) defines a 15-step process that UM reviewers follow. Every step has a home in our pipeline. This mapping is explicit so reviewers reading this spec can see exactly where each step lives.

| # | Workflow Step | Pipeline Component |
|---|---|---|
| 1 | Confirm the request | `CaseContext` construction at intake — captures member, provider, CPT/HCPCS, diagnosis, place of service, service date |
| 2 | Identify the correct guideline | **Policy Selector** (§6.4) — deterministic match on CPT + ICD-10 + payer + LOB + state + date |
| 3 | Classify the request type | `CaseContext` fields `urgency`, `care_setting`, `request_category`, plus `branch` (initial/repeat) from selector |
| 4 | Review clinical documentation | **Intake Agent** (§6.2) — section detection + per-section FHIR extraction with citations |
| 5 | Validate diagnosis and indication | Selector's ICD-10 pattern match + criterion-level diagnosis evaluation in adjudicator |
| 6 | Assess severity and functional impact | Severity criteria in policy tree (e.g., NRS pain, BMI, A1C) evaluated by adjudicator |
| 7 | Check prior treatments / conservative therapy | Step-therapy criteria in policy tree evaluated by adjudicator |
| 8 | Review objective evidence | **Citation contract** — every `met` verdict requires objective patient_evidence with verbatim quote |
| 9 | Check frequency, dosage, level, setting | `applies_to.settings_of_care` filter in selector + frequency/quantity criteria in policy tree |
| 10 | Look for exclusions or contraindications | **Exclusions array** in policy + exclusion short-circuit in determination logic (§6.7) |
| 11 | Compare evidence against criteria | **Adjudicator** (§6.5) — per-criterion four-valued verdicts + **Rollup** (§6.6) — deterministic tree aggregation |
| 12 | Identify missing information | `missing_info[]` array in verdict + **Reviewer Agent** refinement (§6.8) |
| 13 | Make or recommend determination | **Determination logic** (§6.7) — approve/deny/pend mapping with exclusion short-circuit |
| 14 | Document the rationale | **Reviewer Agent** narrative (§6.8) — clinician-to-clinician prose |
| 15 | Communicate the outcome | **PAS Bundle Builder** (§6.9) — ClaimResponse with adjudication + processNote + preAuthRef |

**Three steps need explicit treatment in the build:**

**Step 3 (Classify request type)** — `CaseContext` is extended with three additional fields to support proper classification:

```python
@dataclass
class CaseContext:
    # ... existing fields ...
    urgency: Literal["standard", "urgent", "retrospective"] = "standard"
    care_setting: Literal["outpatient", "inpatient", "asc", "office", "home"] = "outpatient"
    request_category: Literal["procedural", "pharmacy", "dme", "service"] = "procedural"
```

Urgency affects SLA (72h vs 7d per CMS-0057-F). Care setting and request category narrow policy selection. For the Smith case: `urgency="standard"`, `care_setting="outpatient"`, `request_category="procedural"`.

**Step 8 (Objective evidence)** — the Adjudicator's system prompt explicitly requires:

> When a criterion calls for objective evidence (lab value, imaging finding, validated score, documented exam finding), the `met` verdict requires at least one `patient_evidence` entry that is itself objective (Observation, DiagnosticReport, or quoted exam finding from Physical Exam section). Subjective patient report without corroborating documentation is `unclear`, not `met`.

This distinction matters for ESI: "patient reports 9/10 pain" backed by an explicit NRS Observation is objective; the same phrase loose in narrative without a discrete observation is `unclear`.

**Step 9 (Frequency/setting/quantity)** — the criteria tree schema supports a dedicated `evaluator_kind: "frequency_limit"` for leaves that check historical case records:

```json
{
  "id": "C2.frequency.repeat_injection",
  "type": "leaf",
  "description": "No more than 4 injections per region per rolling 12 months",
  "evaluation": {
    "evaluator_kind": "frequency_limit",
    "value_constraints": {
      "max_count": 4,
      "window_months": 12,
      "scope": "same_region"
    },
    "fact_types_needed": ["Procedure", "ClaimHistory"]
  }
}
```

For the prototype with no longitudinal data, frequency criteria return `not_documented` and flag for human review. Production wires this to historical claim records.

### 4.5 What we explicitly do NOT use

- **HAPI FHIR server.** Overkill for prototype; SQLite serves our resources.
- **RAG over policy text.** Policies are decision procedures with logical structure, not knowledge bases. Vector retrieval would hide structure. We store policies as structured trees, not chunks.
- **CQL execution engine.** Standard in production, but criteria-tree + LLM evaluator gives equivalent capability in prototype timeline.
- **X12 278 conversion.** Feb 2024 CMS enforcement discretion makes FHIR-only PA legitimate.
- **CDS Hooks / CRD endpoints.** Upstream of what we demonstrate.
- **US Core profile validation.** Base R4 shape is sufficient; full US Core compliance is weeks.
- **SMART on FHIR / OAuth.** Mock with API keys; auth distracts from the core demo.

---

## 5. Data Models

### 5.1 Policy schema

Every policy is a JSON file in `policies/`. Loaded once at process startup. Treated as code, versioned in git. Schema:

```json
{
  "policy_id": "molina-mcp-032",
  "name": "Epidural Steroid Injections for Back and Neck Pain",
  "payer_id": "molina",
  "version": "2024-08-14",
  "effective_from": "2024-08-14",
  "effective_until": null,
  "source": {
    "pdf_path": "policies/sources/molina-mcp-032.pdf",
    "total_pages": 8,
    "publication_url": "https://www.molinahealthcare.com/..."
  },

  "applies_to": {
    "cpt_codes": ["62321", "62322", "62323", "64479", "64480", "64483", "64484"],
    "hcpcs_codes": [],
    "icd10_patterns": ["M54.*", "M51.*", "M48.0*", "G89.21", "G89.29", "B02.2*"],
    "lines_of_business": ["medicaid", "medicare-advantage"],
    "states": ["NY", "NJ", "CA", "TX", "FL", "MI", "OH", "WA", "UT", "ID", "NM"],
    "age_min": 18,
    "age_max": null,
    "settings_of_care": ["outpatient", "ambulatory-surgery-center"],
    "branches": {
      "initial": "First or diagnostic injection (no prior ESI for this episode)",
      "repeat": "Repeat therapeutic injection — requires documented prior response"
    }
  },

  "criteria": {
    "id": "root",
    "type": "internal",
    "operator": "ALL",
    "description": "Medical necessity criteria for ESI",
    "policy_citation": { "page": 2, "section": "Coverage Policy", "quote": "..." },
    "children": [ /* tree nodes */ ]
  },

  "exclusions": [
    {
      "id": "X1",
      "description": "Non-radicular back pain",
      "policy_citation": { "page": 3, "quote": "..." },
      "verdict_rubric": { /* ... */ }
    }
  ],

  "metadata": {
    "authored_by": "...",
    "authored_at": "2026-05-22",
    "review_status": "human-validated",
    "test_cases": ["case_smith_001"]
  }
}
```

### 5.2 Criteria tree node schemas

**Leaf node:**

```json
{
  "id": "C1.indication.initial.conservative_therapy.failed_pt",
  "type": "leaf",
  "description": "Documented physical therapy ≥4 weeks (3-4 sessions/week, 12 sessions total)",

  "policy_citation": {
    "page": 2,
    "section": "Coverage Policy",
    "quote": "Physical therapy for a minimum of 4 weeks (3-4x per week for a total of 12 sessions)",
    "start_offset": 1423,
    "end_offset": 1502
  },

  "evaluation": {
    "fact_types_needed": ["Procedure", "ServiceRequest", "ClinicalImpression"],
    "extraction_hints": "Look for PT codes (97110, 97140, 97530) or text mentions of physical therapy. Required: start and end dates spanning ≥4 weeks, frequency, total sessions.",
    "value_constraints": {
      "duration_min_weeks": 4,
      "frequency_min_per_week": 3,
      "total_sessions_min": 12
    },
    "evaluator_kind": "llm_with_temporal_constraint"
  },

  "verdict_rubric": {
    "met": "PT was completed with documented dates spanning ≥4 weeks, frequency ≥3x/week, and ≥12 sessions, all in the current episode of pain",
    "not_met": "PT attempt was documented but does NOT meet duration/frequency/session thresholds",
    "unclear": "PT attempted but documentation is ambiguous on duration or frequency, OR alternate criterion path may apply",
    "not_documented": "No PT trial mentioned in the available patient records"
  }
}
```

**Internal node:**

```json
{
  "id": "C1.indication.initial",
  "type": "internal",
  "operator": "ALL",
  "description": "Initial diagnostic injection criteria",
  "policy_citation": {
    "page": 2,
    "section": "Coverage Policy",
    "quote": "For Initial (Diagnostic) injection(s) up to 2 injections, ALL the following are met:"
  },
  "children": [ /* child node IDs/refs */ ]
}
```

**Operators supported:** `ALL`, `ONE_OF`, `NOT`, `AT_LEAST_K` (where K is specified).

### 5.3 FHIR extraction output schema

Every extracted entity carries citations:

```json
{
  "resourceType": "Condition",
  "id": "cond-001",
  "code": {
    "coding": [{ "system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "M54.16", "display": "Radiculopathy, lumbar region" }],
    "text": "lumbar radicular pain"
  },
  "subject": { "reference": "Patient/smith" },
  "_citations": [
    {
      "document_id": "doc_smith_fax_001",
      "page": 1,
      "section": "Fax Cover",
      "quote": "DX Code : M54.16",
      "start_offset": 412,
      "end_offset": 426,
      "extraction_confidence": 0.98
    },
    {
      "document_id": "doc_smith_fax_001",
      "page": 4,
      "section": "History",
      "quote": "left low back pain",
      "start_offset": 1834,
      "end_offset": 1852,
      "extraction_confidence": 0.92
    }
  ]
}
```

`_citations` is a non-standard FHIR extension; we use the underscore prefix convention. Production would use FHIR Provenance resources, but `_citations` is simpler for the prototype.

### 5.4 Case schema

```json
{
  "case_id": "case_a3f2",
  "status": "needs_info",
  "patient": { /* FHIR Patient with _citations */ },
  "coverage": { /* FHIR Coverage */ },
  "extracted_facts": {
    "conditions": [ /* Conditions with _citations */ ],
    "observations": [ /* ... */ ],
    "medications": [ /* ... */ ],
    "procedures": [ /* ... */ ],
    "service_request": { /* ServiceRequest */ }
  },
  "policy_selection": {
    "selected_policy_id": "molina-mcp-032",
    "branch": "initial",
    "candidates_considered": ["molina-mcp-032"],
    "eliminated": [],
    "selection_reason": "CPT 62323 matches; M54.16 matches M54.* pattern; payer=molina; LOB=medicaid; state=NY"
  },
  "criteria_evaluation": {
    "root_verdict": "unclear",
    "leaf_verdicts": { /* id -> verdict */ },
    "tree": [ /* annotated tree */ ]
  },
  "determination": {
    "outcome": "pend",
    "narrative": "...",
    "missing_information": [
      {
        "id": "MI1",
        "criterion_id": "C1.indication.initial.conservative_therapy",
        "request": "Document either (a) attempted completion of 4 weeks of PT with outcomes, OR (b) imaging correlation and clinical rationale for why PT is contraindicated."
      }
    ]
  },
  "pas_response_bundle": { /* full FHIR Bundle */ },
  "audit": {
    "agents_run": [ /* timestamped log */ ],
    "total_latency_ms": 18420,
    "total_tokens": 84230,
    "total_cost_usd": 0.21
  }
}
```

### 5.5 Verdict and evaluation schemas

**Adjudicator output per criterion:**

```json
{
  "criterion_id": "C1.indication.initial.conservative_therapy.failed_pt",
  "verdict": "unclear",
  "confidence": 0.65,
  "patient_evidence": [
    {
      "fhir_resource_id": "Procedure/pt-eval-001",
      "fact_type": "ServiceRequest",
      "value_summary": "PT plan: 20 visits, 10 weeks",
      "document_id": "doc_smith_fax_001",
      "page": 11,
      "section": "Assessment/Plan",
      "quote": "Frequency: 2 sessions per week. Duration: 10 weeks. Visits: 20"
    },
    {
      "fhir_resource_id": "Observation/pt-status-001",
      "value_summary": "PT not started — pain prohibitive",
      "document_id": "doc_smith_fax_001",
      "page": 4,
      "section": "Prior Pain Therapeutic Trials",
      "quote": "Physical Therapy: too painful to start"
    }
  ],
  "reasoning": "PT plan documented (20 visits over 10 weeks) but never executed because patient found it 'too painful to start.' Does not meet completed-4-weeks criterion. Alternate pathway (precluded PT due to radiculopathy) may apply but requires explicit clinical rationale.",
  "missing_info": [
    "Documentation of completed PT with outcomes, OR",
    "Imaging correlation and clinical rationale establishing PT is contraindicated"
  ]
}
```

### 5.6 Verdict definitions (precise)

| Verdict | Definition |
|---|---|
| **met** | Patient documentation contains evidence that affirmatively satisfies the criterion under any reasonable reading |
| **not_met** | Patient documentation contains evidence that affirmatively contradicts the criterion |
| **unclear** | Documentation is partially present but insufficient for a confident verdict; ambiguous values; potential alternate criterion path |
| **not_documented** | No relevant information found in available records |

`unclear` and `not_documented` are distinct missing-info messages: "we found something but it's incomplete" vs. "the chart is silent on this concept."

---

## 6. Component Specifications

### 6.1 Document Ingestion + Sectioning

**Input:** Path to a clinical PDF.
**Output:** A `DocumentReference` plus a list of `Section` records.

**Implementation:**

```python
@dataclass
class Section:
    section_id: str
    section_type: str  # "history", "medications", "assessment", "plan", etc.
    page_number: int
    start_offset: int  # char offset within page
    end_offset: int
    text: str

def ingest_document(pdf_path: str) -> tuple[DocumentReference, list[Section]]:
    # 1. PyMuPDF extracts text + bbox per page
    pages = extract_pages_pymupdf(pdf_path)
    
    # 2. Regex first-pass on canonical section headers
    regex_sections = regex_section_split(pages)
    
    # 3. LLM confirms boundaries and fills gaps
    confirmed_sections = llm_confirm_sections(regex_sections, pages)
    
    # 4. Build DocumentReference (FHIR)
    doc_ref = DocumentReference(
        id=generate_id(),
        content=[{"attachment": {"contentType": "application/pdf", "url": pdf_path}}]
    )
    
    return doc_ref, confirmed_sections
```

**Canonical section types to detect:**
- Demographics / Patient Info
- Chief Complaint
- History of Present Illness (HPI)
- Past Medical History (PMH)
- Past Surgical History (PSH)
- Current Medications
- Allergies
- Physical Exam
- Labs and Imaging
- Prior Pain Therapeutic Trials (case-specific)
- Assessment
- Plan
- Assessment/Plan (combined)
- Visit Diagnoses

**Failure mode:** if a page is scanned (no extractable text), fall back to Tesseract OCR. Log the page as OCR-derived for downstream confidence scoring.

### 6.2 Intake Agent

**Input:** A DocumentReference + Sections.
**Output:** A bundle of FHIR resources with citations.

**Approach:** Two-pass extraction.

**Pass 1 — Per-section, tool-calling extraction.**

For each section, invoke Claude Sonnet 4.5 with the relevant tool subset. Example: a "Medications" section → tools = [`extract_medications`]. A "Physical Exam" section → tools = [`extract_observations`].

Each tool's parameters define the extraction schema. The model fills them out. Every extracted value carries `source_quote`, `source_section_id`, `source_page`, `source_offset_start`, `source_offset_end`.

**Pass 2 — Cross-section consolidation.**

A single LLM call (no tools) takes all per-section extractions and:
- Merges duplicates (same condition mentioned in PMH + HPI → one Condition with two citation entries)
- Links references (a Medication mentioned in PMH with later "discontinued" in Plan → MedicationRequest.status="stopped")
- Resolves the ServiceRequest from the most authoritative source (fax cover sheet preferred)

**Citation verification (hard contract):**

Before any extracted entity is persisted, every `source_quote` must be substring-verified (whitespace-normalized) against the cited document at the cited page. **If verification fails, the entity is dropped, a warning is logged, and a metric increments.** Citation faithfulness target: 100%.

**System prompt — packaged as a Claude Skill at `skills/pa-intake/SKILL.md`:**

> You are a clinical fact extractor. Your only job is to identify clinical facts in the provided document section and structure them as FHIR resources. You do not make clinical inferences. You do not evaluate, judge, or recommend.
>
> Every fact you extract MUST include a verifiable source citation — an exact substring quote from the source document, with section and page. Quotes must be verbatim. If a fact cannot be cited verbatim, do not extract it.
>
> When uncertain about a value, prefer omission over guessing. Downstream evaluation will surface "not_documented" rather than allow a wrong value through.

### 6.3 Policy Registry

**Input:** Directory of policy JSON files at `policies/*.json`.
**Output:** An indexed registry queryable by various attributes.

```python
class PolicyRegistry:
    def __init__(self, policies_dir: Path):
        self._policies: dict[str, Policy] = {}
        self._cpt_index: dict[str, list[str]] = {}  # CPT -> [policy_ids]
        self._load_all(policies_dir)
    
    def get(self, policy_id: str) -> Policy: ...
    def all_policies(self) -> list[Policy]: ...
    def policies_covering_cpt(self, cpt: str) -> list[Policy]: ...
    def policies_for_payer(self, payer_id: str) -> list[Policy]: ...
```

The registry validates every loaded policy against the schema at startup. A malformed policy fails loud, not silent.

### 6.4 Policy Selector

**Input:** A `CaseContext`.
**Output:** A `SelectionResult`.

**Contract:** deterministic, no LLM, runs in ~ms. Never silently picks on ambiguity.

```python
@dataclass
class CaseContext:
    cpt_code: str
    icd10_codes: list[str]
    payer_id: str
    line_of_business: str
    state: str
    patient_age: int
    service_date: date
    branch_hint: str | None  # "initial" / "repeat" if inferable
    # Request classification (Step 3 of the 15-step workflow)
    urgency: Literal["standard", "urgent", "retrospective"] = "standard"
    care_setting: Literal["outpatient", "inpatient", "asc", "office", "home"] = "outpatient"
    request_category: Literal["procedural", "pharmacy", "dme", "service"] = "procedural"

@dataclass
class SelectionResult:
    status: Literal["ok", "no_match", "needs_disambiguation"]
    selected_policy_id: str | None
    candidates_considered: list[str]
    eliminated: list[EliminationRecord]
    branch: str | None

@dataclass
class EliminationRecord:
    policy_id: str
    reason: str

def select_policy(case: CaseContext, registry: PolicyRegistry) -> SelectionResult:
    candidates = []
    eliminated = []

    for policy in registry.all_policies():
        if not policy.is_effective_on(case.service_date):
            eliminated.append(EliminationRecord(policy.policy_id, "not_effective_on_service_date"))
            continue
        if policy.payer_id != case.payer_id:
            continue  # different payer, silently skip
        if case.cpt_code not in policy.applies_to.cpt_codes:
            continue
        if case.line_of_business not in policy.applies_to.lines_of_business:
            eliminated.append(EliminationRecord(policy.policy_id, f"LOB mismatch: case={case.line_of_business}"))
            continue
        if case.state not in policy.applies_to.states:
            eliminated.append(EliminationRecord(policy.policy_id, f"state mismatch: case={case.state}"))
            continue
        if not _icd10_matches_any(case.icd10_codes, policy.applies_to.icd10_patterns):
            eliminated.append(EliminationRecord(policy.policy_id, "no ICD-10 pattern match"))
            continue
        if case.patient_age < (policy.applies_to.age_min or 0):
            eliminated.append(EliminationRecord(policy.policy_id, "under age minimum"))
            continue
        candidates.append(policy)

    if not candidates:
        return SelectionResult(status="no_match", selected_policy_id=None,
                              candidates_considered=[], eliminated=eliminated, branch=None)

    if len(candidates) == 1:
        branch = _determine_branch(case, candidates[0])
        return SelectionResult(status="ok", selected_policy_id=candidates[0].policy_id,
                              candidates_considered=[candidates[0].policy_id],
                              eliminated=eliminated, branch=branch)

    # Specificity disambiguation
    ranked = sorted(candidates, key=lambda p: p.specificity_score(), reverse=True)
    if ranked[0].specificity_score() > ranked[1].specificity_score():
        branch = _determine_branch(case, ranked[0])
        return SelectionResult(status="ok", selected_policy_id=ranked[0].policy_id,
                              candidates_considered=[p.policy_id for p in candidates],
                              eliminated=eliminated, branch=branch)

    # Genuine ambiguity — escalate
    return SelectionResult(status="needs_disambiguation",
                          selected_policy_id=None,
                          candidates_considered=[p.policy_id for p in candidates],
                          eliminated=eliminated, branch=None)
```

**Helper:** `_icd10_matches_any(codes, patterns)` supports glob patterns like `M54.*` which matches any code starting with "M54.".

**Specificity score:** count of constraints in `applies_to`. More constraints = more specific. Diagnosis-specific policy beats general one for the same CPT.

**Branch determination:** for the prototype, defaults to `initial` if no prior case history exists. Future enhancement: query historical cases for prior approvals on the same CPT + diagnosis + episode.

### 6.5 Adjudicator Agent

**Input:** One leaf criterion + filtered patient facts.
**Output:** A verdict object (see §5.5).

**Approach:** parallel execution across all leaves in the selected policy's tree.

```python
async def evaluate_all_leaves(policy: Policy, facts: PatientFacts) -> dict[str, Verdict]:
    leaves = policy.criteria.flatten_leaves()
    tasks = [evaluate_leaf(leaf, facts) for leaf in leaves]
    verdicts = await asyncio.gather(*tasks)
    return {leaf.id: v for leaf, v in zip(leaves, verdicts)}

async def evaluate_leaf(criterion: Leaf, facts: PatientFacts) -> Verdict:
    # Filter facts to only those relevant to this criterion
    relevant_facts = facts.filter_by_types(criterion.evaluation.fact_types_needed)
    
    # Build prompt with cached portions
    response = await claude_client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=2000,
        system=[
            {"type": "text", "text": ADJUDICATOR_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": format_tools(ADJUDICATOR_TOOLS), "cache_control": {"type": "ephemeral"}},
        ],
        tools=ADJUDICATOR_TOOLS,
        messages=[{
            "role": "user",
            "content": format_evaluation_request(criterion, facts.case_id, relevant_facts)
        }]
    )
    
    verdict = parse_verdict(response)
    verify_citations(verdict, facts)  # hard contract
    return verdict
```

**Tool definitions (for the Adjudicator):**

```python
ADJUDICATOR_TOOLS = [
    {
        "name": "search_facts_by_type",
        "description": "Retrieve all patient facts of a given FHIR resource type. Use when the relevant facts subset is insufficient.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fact_type": {"type": "string", "enum": ["Condition", "Observation", "MedicationRequest", "Procedure", "ServiceRequest"]}
            },
            "required": ["fact_type"]
        }
    },
    {
        "name": "get_document_excerpt",
        "description": "Get the surrounding context for a specific cited span in a source document.",
        "input_schema": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "page": {"type": "integer"},
                "section": {"type": "string"}
            },
            "required": ["document_id", "page"]
        }
    },
    {
        "name": "check_temporal_constraint",
        "description": "Verify a duration or frequency constraint against dates in the chart. E.g., 'PT for ≥4 weeks' with start/end dates.",
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "format": "date"},
                "end_date": {"type": "string", "format": "date"},
                "min_weeks": {"type": "number"},
                "min_frequency_per_week": {"type": "number"}
            },
            "required": ["start_date", "end_date"]
        }
    }
]
```

**System prompt — Claude Skill at `skills/pa-adjudicator/SKILL.md`:**

> You are evaluating ONE prior authorization criterion against patient data. You return one of four verdicts: `met`, `not_met`, `unclear`, `not_documented`.
>
> Definitions:
> - `met`: Patient documentation contains evidence that affirmatively satisfies the criterion under any reasonable reading.
> - `not_met`: Patient documentation contains evidence that affirmatively contradicts the criterion.
> - `unclear`: Documentation is partially present but insufficient for a confident verdict; values present but conflicting; alternate path may apply.
> - `not_documented`: No relevant information found in available records.
>
> CRITICAL RULES:
> 1. Cite every piece of patient evidence with an exact verbatim quote from the source document.
> 2. Never invent citations. If a quote cannot be produced verbatim from the chart, do not cite it.
> 3. When in doubt between `met` and `unclear`, choose `unclear` and specify what additional documentation would resolve the question.
> 4. You evaluate one criterion in isolation. You do not see other criteria. You do not make global determinations.
> 5. **Objective evidence requirement**: When a criterion calls for objective evidence (lab value, imaging finding, validated clinical score, documented exam finding), a `met` verdict requires at least one `patient_evidence` entry that is itself objective — an Observation, DiagnosticReport, or quoted finding from a Physical Exam / Imaging / Labs section. Subjective patient self-report without corroborating documentation is `unclear`, not `met`.

**Prompt caching:**
- System prompt: cached
- Tool definitions: cached
- Policy criterion text: cached per case (same across criteria)
- Patient facts: not cached (case-specific)

Expected cost reduction: ~80% on the cacheable portions, ~$0.10–0.20 per case total.

### 6.6 Tree Rollup

Pure deterministic Python. No LLM. The verdict aggregation logic:

```python
def rollup(node: Node, leaf_verdicts: dict[str, Verdict]) -> str:
    if node.type == "leaf":
        return leaf_verdicts[node.id].verdict
    
    child_verdicts = [rollup(c, leaf_verdicts) for c in node.children]
    
    if node.operator == "ALL":
        if all(v == "met" for v in child_verdicts):
            return "met"
        if any(v == "not_met" for v in child_verdicts):
            return "not_met"
        return "unclear"
    
    if node.operator == "ONE_OF":
        if any(v == "met" for v in child_verdicts):
            return "met"
        if all(v == "not_met" for v in child_verdicts):
            return "not_met"
        return "unclear"
    
    if node.operator == "NOT":
        single = child_verdicts[0]
        return {"met": "not_met", "not_met": "met"}.get(single, "unclear")
    
    if node.operator == "AT_LEAST_K":
        met_count = sum(1 for v in child_verdicts if v == "met")
        if met_count >= node.k:
            return "met"
        # ... etc
    
    raise ValueError(f"Unknown operator: {node.operator}")
```

### 6.7 Determination

Combines root verdict with exclusion check:

```python
def determine(root_verdict: str, exclusion_verdicts: list[str]) -> str:
    if any(v == "met" for v in exclusion_verdicts):
        return "deny"  # exclusion short-circuits
    if root_verdict == "met" and all(v == "not_met" for v in exclusion_verdicts):
        return "approve"
    if root_verdict == "not_met":
        return "deny"
    return "pend"
```

### 6.8 Reviewer Agent

**Input:** Full case state (patient facts, criteria tree with verdicts, deterministic determination).
**Output:** Narrative + refined missing-info requests + optional human-review flag.

**Tools:**
- `get_policy_section(section_id)` — look up specific policy text for context
- `get_patient_facts(filter)` — drill into facts
- `get_document_excerpt(span)` — get surrounding source context
- `draft_clinician_question(topic)` — produce a clear question for the provider
- `flag_for_human_review(reason)` — escalate edge case

**Constraint:** the Reviewer cannot override the structural verdict. Even if it disagrees, it must flag rather than flip.

**System prompt — Claude Skill at `skills/pa-reviewer/SKILL.md`:**

> You are the final reviewer for a prior authorization case. The deterministic rules engine has produced a structural verdict; your job is to write a clear, professional narrative explaining the verdict to the medical director and (where applicable) to refine missing-information requests into actionable language for the ordering clinician.
>
> You do NOT change the verdict. If you believe the verdict is wrong, use `flag_for_human_review` with your reasoning.
>
> Style: clinician-to-clinician. Specific, not generic. Reference the policy by section. Reference patient evidence by document and date.
>
> Missing-info requests should be answerable in one provider response. Don't ask vague questions; ask specific ones with clear "what would resolve this" language.

### 6.9 PAS Bundle Builder

**Input:** Case with completed determination.
**Output:** FHIR Bundle conforming to Da Vinci PAS profile.

The Bundle contains:
- `ClaimResponse` (the determination)
- `Patient` (echoed back)
- `Coverage`
- `Practitioner` (the reviewer, if applicable)
- `Organization` (the payer)

The `ClaimResponse.item[].adjudication` array carries per-CPT decisions. The `ClaimResponse.processNote` array carries the criterion-by-criterion rationale. The `ClaimResponse.preAuthRef` provides the authorization number on approve.

```python
def build_pas_response(case: Case) -> Bundle:
    claim_response = ClaimResponse(
        status="active",
        type=CodeableConcept(coding=[Coding(system="http://terminology.hl7.org/CodeSystem/claim-type", code="professional")]),
        use="preauthorization",
        patient=Reference(reference=f"Patient/{case.patient.id}"),
        outcome=_map_outcome(case.determination.outcome),  # queued/complete/partial/error
        disposition=case.determination.narrative,
        preAuthRef=case.case_id if case.determination.outcome == "approve" else None,
        item=[_build_response_item(case)],
        processNote=[_build_process_note(c) for c in case.criteria_evaluation.leaf_verdicts.values()]
    )
    bundle = Bundle(
        type="collection",
        entry=[
            BundleEntry(resource=claim_response),
            BundleEntry(resource=case.patient),
            BundleEntry(resource=case.coverage),
        ]
    )
    return bundle
```

### 6.10 MCP Server

Mounted on the same FastAPI process at `/mcp` using Streamable HTTP transport.

**Tools exposed:**

| Tool | Purpose |
|---|---|
| `list_patients()` | List known patients |
| `get_patient(id)` | Return FHIR Patient + Coverage |
| `get_clinical_documents(patient_id)` | List documents |
| `extract_clinical_facts(document_id)` | Force re-extraction; return FHIR + citations |
| `list_policies()` | List loaded policies |
| `get_policy(policy_id)` | Return criteria tree |
| `evaluate_prior_auth(patient_id, cpt_code, policy_id?)` | Run full pipeline |
| `evaluate_criterion(criterion_id, patient_id)` | Drill-down single criterion eval |
| `submit_pas_claim(bundle)` | Accept Da Vinci PAS Bundle, return ClaimResponse |
| `draft_appeal_letter(case_id)` | Draft an appeal for denied/pended case |

**Resources exposed (URI-addressable):**

| URI scheme | Returns |
|---|---|
| `patient://{id}` | Patient FHIR resource |
| `note://{id}` | Raw note text + section index |
| `policy://{id}` | Policy criteria JSON |
| `case://{id}` | Full case state |
| `case://{id}/timeline` | Audit log |

**Prompts:**

| Prompt | Purpose |
|---|---|
| `pa_review_summary` | "Summarize PA case in 3 sentences for a clinician" |
| `appeal_letter_draft` | "Draft an appeal letter for a denied case" |
| `clinician_question` | "Ask the provider for specific missing info" |

### 6.11 REST API

```
POST   /v1/documents                       Upload a clinical document
GET    /v1/documents/{id}                  Get document metadata + sections
POST   /v1/documents/{id}/extract          Run / re-run extraction

GET    /v1/patients/{id}                   Get FHIR Patient
GET    /v1/patients/{id}/everything        Get all resources for patient

GET    /v1/policies                        List policies
GET    /v1/policies/{id}                   Get criteria tree

POST   /v1/cases                           Create a PA case
GET    /v1/cases/{id}                      Get case state
POST   /v1/cases/{id}/evaluate             Run evaluation pipeline
GET    /v1/cases/{id}/determination        Get current determination

POST   /fhir/Claim/$submit                 Da Vinci PAS submission endpoint
GET    /fhir/ClaimResponse/{id}            Get PAS response Bundle

ALL    /mcp/*                              MCP server (Streamable HTTP)

GET    /.well-known/agent.json             A2A Agent Card (stretch)

GET    /metrics                            Prometheus metrics
GET    /health                              Health check
```

### 6.12 Demo UI

**Decision:** Streamlit by default; React only if time permits on Day 6.

**Essential screens (must build):**

**Case List (Inbox):**
- Status-badged queue
- Columns: status | patient | service | payer | age | last update
- Filterable by status; sortable by age

**Case Detail (Workspace) — the demo screenshot:**
- Three-panel layout:
  - Left: PDF preview with citation highlights
  - Center: structured FHIR data, each fact linking back to PDF span
  - Right: criteria tree with met/not_met/unclear/not_documented badges, expandable to show evidence + reasoning + policy quote
- Below the panels: agent narrative + missing-info requests + action buttons (Approve / Deny / Send Info Request / Override)
- Click a fact → PDF scrolls and highlights source span
- Click a criterion → expands evidence and reasoning

**Submit Case:**
- Drag-and-drop PDF
- Optional metadata: known CPT, payer
- Submit → progress indicator → lands on Case Detail when complete

**Stretch screens (nice to have):**
- **Policy Browser:** visualize criteria tree, show each leaf's source quote + page
- **Audit Trail:** timeline of every agent call for a case, with latencies and token costs

---

## 7. Project Layout

```
latitude-health-pa/
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── Dockerfile
├── fly.toml                              (deployment)
│
├── app/
│   ├── main.py                           FastAPI entry
│   ├── settings.py
│   │
│   ├── api/                              HTTP route handlers
│   │   ├── documents.py
│   │   ├── patients.py
│   │   ├── policies.py
│   │   ├── cases.py
│   │   ├── fhir_pas.py                   /fhir/Claim/$submit
│   │   └── a2a.py                        Agent Card (stretch)
│   │
│   ├── mcp/                              MCP server
│   │   ├── server.py
│   │   ├── tools.py
│   │   ├── resources.py
│   │   └── prompts.py
│   │
│   ├── extraction/                       Stage 1-2
│   │   ├── pdf.py                        PyMuPDF + OCR fallback
│   │   ├── sections.py                   regex + LLM sectioning
│   │   └── intake_agent.py               FHIR extraction with citations
│   │
│   ├── policy/                           Stage 3-4
│   │   ├── registry.py                   loader + index
│   │   ├── selector.py                   the policy selector
│   │   ├── tree.py                       criteria tree data structures
│   │   └── adjudicator.py                per-criterion LLM eval
│   │
│   ├── determination/                    Stage 5
│   │   ├── rollup.py                     deterministic tree rollup
│   │   ├── reviewer_agent.py             narrative + missing-info
│   │   └── decider.py                    final outcome mapping
│   │
│   ├── pas/                              Stage 6
│   │   ├── bundle_builder.py             build ClaimResponse Bundle
│   │   └── bundle_parser.py              parse incoming Claim Bundle
│   │
│   ├── models/                           Pydantic + DB models
│   │   ├── case.py
│   │   ├── policy.py
│   │   ├── fhir.py                       wrappers around fhir.resources
│   │   └── verdict.py
│   │
│   ├── db/                               SQLAlchemy
│   │   ├── engine.py
│   │   └── repositories.py
│   │
│   ├── llm/                              LLM client
│   │   ├── client.py                     Anthropic wrapper + caching
│   │   ├── prompts.py                    prompt templates
│   │   └── citation_verify.py            substring verification
│   │
│   └── observability/
│       ├── logging.py                    structlog setup
│       ├── tracing.py                    OpenTelemetry
│       ├── langfuse_export.py
│       └── metrics.py                    Prometheus
│
├── skills/                               Claude Skills
│   ├── pa-intake/
│   │   ├── SKILL.md
│   │   ├── fhir-shapes.md
│   │   └── citation-rules.md
│   ├── pa-adjudicator/
│   │   ├── SKILL.md
│   │   ├── verdict-rubric.md
│   │   └── policy-vocabulary.md
│   └── pa-reviewer/
│       ├── SKILL.md
│       ├── tone-guide.md
│       └── missing-info-templates.md
│
├── policies/
│   ├── molina-mcp-032.json               criteria tree (the keystone)
│   └── sources/
│       └── molina-mcp-032.pdf
│
├── data/
│   ├── pdfs/                             source PDFs
│   ├── extracted/                        cached extractions
│   └── app.db                            SQLite (gitignored)
│
├── frontend/                             Streamlit (or React)
│   ├── app.py                            Streamlit entry
│   ├── pages/
│   │   ├── 01_inbox.py
│   │   ├── 02_case_detail.py
│   │   └── 03_submit.py
│   └── components/
│       ├── pdf_viewer.py
│       ├── fhir_panel.py
│       └── criteria_tree.py
│
├── tests/
│   ├── unit/
│   │   ├── test_selector.py
│   │   ├── test_rollup.py
│   │   └── test_citation_verify.py
│   ├── integration/
│   │   └── test_smith_e2e.py
│   ├── evals/
│   │   ├── level1_selector/
│   │   ├── level2_criteria/
│   │   └── level3_e2e/
│   └── fixtures/
│       ├── smith_case.pdf
│       └── synthetic/
│
└── scripts/
    ├── seed_smith_case.py                pre-load Smith for demo
    ├── run_evals.py
    └── author_policy.py                  helper for policy authoring
```

---

## 8. Build Phasing

Seven days of focused work. Each day's deliverable is testable independently.

### Day 1 — Foundation

- Bootstrap FastAPI project with structure above
- `pyproject.toml`, `Dockerfile`, `docker-compose.yml` for local dev
- Settings via pydantic-settings (env vars)
- Structured logging via structlog
- Health check endpoint
- SQLite schema + SQLAlchemy models for cases, documents, policies
- README walkthrough

**Acceptance:** `docker compose up` brings up a working API with `/health` returning 200.

### Day 2 — The keystone artifact: Molina ESI Policy 032

This day is dedicated to authoring `policies/molina-mcp-032.json`.

- Walk the Molina policy PDF page by page
- Capture every criterion as a leaf node with the full schema (description, policy_citation with verbatim quote, evaluation hints, value constraints, verdict rubric)
- Capture every exclusion
- Capture the `applies_to` metadata
- Build the criteria tree with correct operators (ALL, ONE_OF, etc.)
- Validate the JSON against the policy schema
- Write Policy Registry that loads and indexes it

**Acceptance:** `policies/molina-mcp-032.json` exists with ~30 leaves, validates, loads into the registry without errors. Every criterion has a verbatim policy_citation that can be substring-verified against the source PDF.

This is the **most leveraged 4–6 hours in the build.** Every downstream component evaluates against this artifact.

### Day 3 — Ingestion + Intake

- PDF ingestion with PyMuPDF (page/bbox preserved)
- Section detection (regex first-pass + LLM confirmation)
- Intake Agent with full tool definitions
- Citation verification (the hard contract)
- Test on the Smith case PDF

**Acceptance:** Smith PDF processed end-to-end produces a list of FHIR resources, every entity has verifiable citations, citation_verification passes 100%, the ServiceRequest correctly extracts CPT 62323 + M54.16.

### Day 4 — Selector + Adjudicator

- Implement the Policy Selector with all the eliminate-and-record logic
- Implement Level 1 selector eval set (15 test cases) and run it
- Implement the Adjudicator Agent with tool definitions
- Parallel async execution across all leaves
- Prompt caching for system + tools + policy criterion text
- Tree rollup + exclusion check
- Test on the Smith case

**Acceptance:** Smith case produces expected per-criterion verdicts. Selector eval passes ≥14/15. The Smith case's `C1.indication.initial.conservative_therapy.failed_pt` evaluates to `unclear` (or `not_met` per the strict reading). The Smith case's `X02` exclusion evaluates to `unclear`. The deterministic rollup produces `unclear` at root → `pend` determination.

### Day 5 — Reviewer + PAS Bundle + MCP

- Reviewer Agent with narrative + missing-info refinement
- PAS Bundle builder
- `/fhir/Claim/$submit` endpoint accepting Bundles and returning ClaimResponse Bundles
- MCP server with all tools + resources + prompts
- A2A Agent Card at `/.well-known/agent.json` (stretch)
- Audit trail persistence

**Acceptance:** End-to-end run on Smith case produces:
- Correct determination (`pend`)
- Two specific missing-info requests matching the expected wording
- Valid PAS Bundle that conforms to the Da Vinci PAS shape
- MCP server reachable from a separate Claude client (e.g., Claude Desktop) and the same case is reproducible via MCP calls

### Day 6 — UI + Evals

- Streamlit UI: Case List, Case Detail (with PDF + FHIR + criteria tree), Submit
- Wire up citation click-through (FHIR fact ↔ PDF span)
- Wire up criterion expansion (verdict + evidence + reasoning + policy quote)
- Level 2 criterion eval set (~30 test cases)
- CI integration: every PR runs Level 1 + Level 2 + Level 3 snapshot
- Eval results dashboard

**Acceptance:** Case Detail screen is screenshot-ready. Eval suite runs in <10 minutes, produces a structured report. Smith case end-to-end snapshot matches expected output.

### Day 7 — Polish + Deck

- Deploy to Fly.io
- 5-minute demo video walkthrough
- 8-slide deck (assignment requirement)
- README finalization with setup instructions
- One-page architecture diagram for the deck

**Acceptance:** Public Fly.io URL works. Deck is presentable. Demo video shows: upload PDF → extracted FHIR with citations → criteria evaluation → pend determination with specific info requests → PAS Bundle output. Show MCP server being called from a Claude client (screenshot or live).

---

## 9. Evaluation Framework

### 9.1 The eval pyramid

```
┌──────────────────────────────────────────────────────────┐
│ Level 3: End-to-end determination eval                   │
│   ~10 labeled cases; expected outcome + key info reqs    │
├──────────────────────────────────────────────────────────┤
│ Level 2: Criterion mapping eval                           │
│   ~30 (criterion, facts) pairs                            │
│   labeled with expected verdict + citations               │
├──────────────────────────────────────────────────────────┤
│ Level 1: Policy selector eval                             │
│   ~15 (case context) → expected policy mappings          │
├──────────────────────────────────────────────────────────┤
│ Level 0: Citation faithfulness (every output, CI)        │
│   substring verification, target 100%                     │
└──────────────────────────────────────────────────────────┘
```

### 9.2 Level 0 — Citation Faithfulness (always-on, automated)

For every quote in every citation (`policy_citation` or `patient_evidence`) produced by any agent:
1. Load the cited document.
2. Normalize whitespace and case.
3. Verify the quote is a substring at the cited page.
4. If yes: pass. If no: fail, log, drop the entity, increment counter.

**Metric: `faithfulness_pass_rate = passed / total`. Target: 100%. Any non-100% is a P0 bug.**

### 9.3 Level 1 — Policy Selector Eval (15 cases)

| # | Category | Case | Expected |
|---|---|---|---|
| S01 | Happy path | CPT 62323, M54.16, Molina Medicaid NY, 50yo, 2026-04-08 | ok → molina-mcp-032 |
| S02 | Different CPT, same policy | CPT 64483, M54.17, Molina Medicaid NY, 45yo | ok → molina-mcp-032 |
| S03 | CPT not in any policy | CPT 99999, M54.16, Molina Medicaid NY | no_match |
| S04 | Wrong diagnosis for CPT | CPT 62323, K35.20, Molina Medicaid NY | no_match |
| S05 | LOB mismatch | CPT 62323, M54.16, Molina Commercial NY | no_match |
| S06 | State outside footprint | CPT 62323, M54.16, Molina Medicaid PA | no_match |
| S07 | Under age limit | CPT 62323, M54.16, Molina Medicaid NY, 16yo | no_match |
| S08 | Service date before policy effective | CPT 62323, M54.16, service 2024-06-01 | no_match (or earlier version) |
| S09 | Service date after policy retired | CPT 62323, M54.16, service 2028-01-01 | no_match (or successor version) |
| S10 | Two diagnoses, one qualifies | CPT 62323, [M54.16, M79.18] | ok → molina-mcp-032 |
| S11 | Specificity disambiguation | CPT covered by general + specific policy | ok → more specific |
| S12 | Genuine ambiguity | Two equal-specificity policies | needs_disambiguation |
| S13 | Repeat branch | CPT 62323, M54.16, prior PA 60 days ago | ok, branch=repeat |
| S14 | Initial branch | CPT 62323, M54.16, no prior history | ok, branch=initial |
| S15 | Wrong payer | CPT 62323, M54.16, Aetna | no_match in Molina registry |

**Metrics:**
- **Selection accuracy on unambiguous cases:** ≥98% target
- **Safe escalation rate on ambiguous:** 100% target
- **False auto-pick rate:** 0% target — any non-zero is a P0 bug

### 9.4 Level 2 — Criterion Mapping Eval (~30 cases)

Organized by category. Each row is a test case.

**Categorical/coded criteria:**
| # | Criterion | Patient facts | Expected verdict |
|---|---|---|---|
| C01 | C1.age: ≥18 | DOB 1975-11-02, service 2026-04-08 | met |
| C02 | C1.age: ≥18 | DOB 2010-05-01, service 2026-04-08 | not_met |
| C03 | C1.indication.initial.diagnosis | M54.16 | met |
| C04 | C1.indication.initial.diagnosis | K35.20 (appendicitis) | not_met |
| C05 | C1.indication.initial.diagnosis (post-surgical) | G89.21, prior laminectomy 2024-10 (8mo prior) | met |
| C06 | C1.indication.initial.diagnosis (post-surgical) | G89.21, prior laminectomy 2026-01 (3mo prior) | not_met |

**Numerical threshold criteria:**
| # | Criterion | Patient facts | Expected verdict |
|---|---|---|---|
| C10 | NRS >4 | Observation NRS 9/10 | met |
| C11 | NRS >4 | Observation NRS 3/10 | not_met |
| C12 | NRS >4 | Observation NRS 5/10 | met (boundary) |
| C13 | NRS >4 | Pain described as "severe", no NRS | unclear |
| C14 | NRS >4 | No pain assessment | not_documented |

**Temporal/duration criteria:**
| # | Criterion | Patient facts | Expected verdict |
|---|---|---|---|
| C20 | PT ≥4 weeks, 3-4x/wk, 12 sessions | PT plan 20 visits/10 weeks, completed | met |
| C21 | PT ≥4 weeks, 3-4x/wk, 12 sessions | PT plan 8 visits/4 weeks (low freq) | not_met |
| C22 | PT ≥4 weeks, 3-4x/wk, 12 sessions | PT plan 12 visits/6 weeks (boundary) | met |
| C23 | PT ≥4 weeks, 3-4x/wk, 12 sessions | PT documented, no dates | unclear |
| C24 | PT ≥4 weeks, 3-4x/wk, 12 sessions | PT ongoing, not yet 4 weeks elapsed | not_met |
| **C25** | **PT ≥4 weeks, 3-4x/wk, 12 sessions** | **"too painful to start" (Smith)** | **unclear** |

**Synonyms and class membership:**
| # | Criterion | Patient facts | Expected verdict |
|---|---|---|---|
| C30 | Conservative drug therapy: NSAIDs/etc. | Ibuprofen 800mg TID | met (ibuprofen IS NSAID) |
| C31 | Conservative drug therapy: NSAIDs/etc. | Acetaminophen only | not_met |
| C32 | Conservative drug therapy: NSAIDs/etc. | Ibuprofen + cyclobenzaprine + gabapentin | met |
| C33 | Conservative drug therapy: NSAIDs/etc. | "OTC pain reliever" unspecified | unclear |

**Negation and partial attempts:**
| # | Criterion | Patient facts | Expected verdict |
|---|---|---|---|
| C40 | PT failed | "completed 12 sessions, no improvement" | met |
| C41 | PT failed | "completed PT with significant improvement" | not_met |
| C42 | PT failed | "stopped after 2 sessions" | unclear |
| C43 | PT failed | "never started PT" | not_documented |

**Cross-document reconciliation:**
| # | Criterion | Patient facts | Expected verdict |
|---|---|---|---|
| C50 | Primary diagnosis radiculopathy | H&P: lumbar radicular pain. Visit dx: M54.16 + M79.18 | unclear |
| C51 | Functional improvement ≥50% maintained 6wk | Note 2026-02-20: pain 9→4. Note 2026-03-15: pain back to 8 | not_met |

**Exclusions:**
| # | Exclusion | Patient facts | Expected verdict |
|---|---|---|---|
| X01 | Myofascial pain primary | Condition M54.16 only | not_met |
| X02 | Myofascial pain primary | Condition M79.18 only | met (deny) |
| **X03** | **Myofascial pain primary** | **M54.16 + M79.18 (Smith)** | **unclear** |
| X04 | Non-radicular back pain | M54.5 | met (deny) |

**Metrics:**
- **Verdict accuracy on clear cases:** ≥92%
- **False positive rate (approve invalid):** <2%
- **Citation precision:** ≥95%
- **Citation recall:** ≥80%
- **Calibration on unclear:** ≥75% of system-`unclear` verdicts are truly ambiguous per human review

### 9.5 Level 3 — End-to-End Determination Eval (10 cases)

| # | Case | Expected determination | Expected info requests |
|---|---|---|---|
| **E01** | **Smith case** | **pend** | **(1) PT completion or contraindication; (2) confirm primary indication** |
| E02 | Clean approve: 55yo, NRS 7, completed PT failed, NSAIDs tried | approve | — |
| E03 | Clear deny: 65yo with primary M79.18 only | deny | — |
| E04 | Repeat injection, no documented prior response | pend | Prior outcome documentation |
| E05 | Post-laminectomy syndrome, 8mo post-op | approve | — |
| E06 | Pediatric 16yo with radicular pain | deny (age) | — |
| E07 | Cervical radicular pain, PT failed, NRS 8 | approve | — |
| E08 | 5th ESI in 12 months | deny (frequency) | — |
| E09 | Wrong CPT for service | needs_human_review | — |
| E10 | Sparse documentation (1-pg consult) | pend with many requests | Multiple |

**Metrics:**
- **Determination exact match:** ≥85%
- **Key info request precision:** ≥80%
- **Latency P95:** <30s
- **Cost per case:** <$0.30

### 9.6 Test case authoring methodology

- **Selector tests:** generate from attribute combination table; no clinical judgment needed.
- **Criterion tests:** 5 minutes per criterion × ~30 criteria = ~2.5 hours.
- **End-to-end tests:** Smith plus 9 synthesized cases; ~30 min per case for synthesis + annotation.
- **Ground truth:** for the prototype, the policy tree author also authors test cases; in production, medical directors review.

### 9.7 CI integration

Every PR runs:
- All Level 0 (citation faithfulness) — always
- All Level 1 (selector) — always, fast
- All Level 2 (criterion) — always, ~5 min
- Snapshot of Level 3 (Smith + 2 others) — diff on regression

Eval delta is part of PR review. Regressions block merge.

### 9.8 Eval results to publish in the deck

```
Policy Matching Eval Results
─────────────────────────────────────────────────
Level 0 — Citation faithfulness:        100% ✓

Level 1 — Selector accuracy:           14/15  (93%)
  False auto-pick rate:                0/15   (0%) ✓
  Safe escalation rate:                3/3    (100%) ✓

Level 2 — Criterion mapping accuracy:
  Categorical/coded:                   6/6    (100%)
  Numerical thresholds:                5/5    (100%)
  Temporal/duration:                   5/6    (83%)
  Synonyms/class:                      4/4    (100%)
  Negation/partial:                    4/4    (100%)
  Cross-document:                      1/2    (50%)
  Exclusions:                          4/4    (100%)
  Overall:                            29/31   (94%)

  Citation precision:                  100%
  Citation recall:                     87%

Level 3 — End-to-end:
  Smith (pend):                        ✓
  Synthesized clean approve:           ✓
  Synthesized clean deny:              ✓

  Latency P95:                          22s
  Cost per case:                       $0.18
```

---

## 10. Observability & Operations

### 10.1 Three telemetry pillars

**Logs:** structlog JSON with `trace_id`, `case_id`, `agent`, `stage`. PHI scrubber on egress (replaces names, DOBs, MRNs).

**Traces:** OpenTelemetry SDK. Every API request, agent invocation, LLM call, tool call as nested spans. Local Jaeger or hosted (Honeycomb/Datadog).

**Metrics:** Prometheus-shaped at `/metrics`:
- `case_processing_duration_seconds` (histogram, labels: stage)
- `llm_calls_total` (counter, labels: agent, model)
- `llm_tokens_total` (counter, labels: agent, direction, cached)
- `llm_cache_hit_ratio` (gauge)
- `citation_verification_failures_total` (counter)
- `agent_tool_call_count` (histogram, labels: agent)
- `determination_outcomes_total` (counter, labels: outcome)

### 10.2 LLM-specific observability with Langfuse

For every LLM call:
- Full prompt (with cached portions marked)
- Full response (including tool calls)
- Token counts in/out + cached
- Latency
- Model + temperature + thinking budget
- Cost
- Trace ID linking to case

Provides per-trace UI, aggregate dashboards, prompt evaluation tracking.

### 10.3 Reliability — retries and idempotency

| Failure | Action |
|---|---|
| API 5xx / timeout | Exponential backoff: 1s, 2s, 4s, 8s, max 4 retries |
| API 429 | Honor retry-after; backoff with jitter |
| Tool output schema invalid | One corrective retry |
| Citation verification fail | Retry once; second failure drops entity + warning |
| Adjudicator unparseable verdict | Retry; second failure marks `unclear` + flags human review |
| Whole pipeline fail | Persist last stage; expose `case.status="error"`; manual retry endpoint |

Every stage writes output to store before next stage begins. Re-running from a stage is a single call: `POST /v1/cases/{id}/retry?from_stage=adjudication`.

### 10.4 Timeouts

| Operation | Soft | Hard |
|---|---|---|
| Single LLM call | 60s | 120s |
| Single tool call | 5s | 15s |
| Full case evaluation | 60s | 180s |

At hard timeout: mark `pending_review` with partial work preserved.

### 10.5 Context management

Sonnet 4.5 context: 200K. Our usage:

| Agent | Typical context | Headroom |
|---|---|---|
| Intake (per section) | ~3K | 60x |
| Adjudicator (per criterion) | ~5K | 40x |
| Reviewer (full case) | ~25K | 8x |

We always chunk per-section, per-criterion. We never put a whole document or whole policy in a single prompt. Prompt caching applied to: system prompts, tool definitions, policy criterion text.

### 10.6 Scale math

Per case: ~40 LLM calls, ~18s end-to-end, ~$0.20 with caching.

At Anthropic Tier 4 rate limits (~10K req/min, 10M tokens/min output):
- Theoretical: 125 cases/min, ~180K/day
- Sufficient for a regional Medicaid managed care plan

Levers if needed: more parallelism, batch API for non-urgent, smaller models for simple criteria (Haiku for "age ≥18"), pre-classification fast-path.

### 10.7 Deployment

**Local:** `docker-compose up` → FastAPI + Streamlit + Langfuse on local ports.

**Demo:** Fly.io. Single Dockerfile. Persistent volume for SQLite. `fly deploy`. ~$0 cost.

**Production (described, not built):** Stateless FastAPI pods + worker pool, Postgres, Redis, S3-equivalent for PDFs, VPC isolation, BAA with Anthropic, full Langfuse + OpenTelemetry deployment.

---

## 11. Production Roadmap (Explicit Cuts)

These are NOT in the prototype but ARE in the production story (mentioned in the deck):

| Cut | Reason | Production answer |
|---|---|---|
| US Core profile compliance | Weeks of validator work | HAPI FHIR + IG validation in CI |
| Real OAuth/SMART on FHIR | Distracting auth setup | SMART app launch with EHR context |
| CQL execution engine | Per-policy authoring weeks | CQL leaves alongside LLM leaves in same tree |
| HAPI FHIR server | Overkill | Production FHIR persistence; multi-tenant |
| Terminology services | Heavy infra | Snowstorm (SNOMED), ValueSet expansion |
| CDS Hooks / CRD endpoints | Upstream of demo | CRD card responses on `order-sign` |
| DTR Questionnaires | Tree subsumes for our purposes | SMART app with auto-populated QuestionnaireResponse |
| X12 278 conversion | CMS enforcement discretion | Optional bridge module |
| Multi-tenant isolation | Prototype is single-tenant | Per-payer logical isolation |
| HA / multi-region | Single-node | Standard cloud-native HA |
| Audit trail immutability | Append-only suffices for demo | WORM storage or cryptographic chaining |
| Continuous eval beyond CI | One-shot manual now | Nightly eval, drift detection, ground-truth feedback loop |
| Human-in-the-loop UI for medical directors | Cuts to UM nurse only | Director queue with structured pre-assembly |
| Cross-policy reuse | Each policy authored standalone | Composable trees with payer-specific overrides |
| Patient longitudinal data | Single-case scope | Historical case query for repeat-injection branches |

---

## 12. Open Configuration Decisions

Decisions to make before or during build that don't have a single right answer:

1. **Reviewer Agent override authority:** can it override the deterministic verdict? **Default: no.** Annotate + flag for human review only.

2. **MCP transport mode:** local stdio (Claude Desktop friendly) or remote Streamable HTTP. **Default: remote**, mounted on FastAPI process.

3. **UI choice:** Streamlit (Day 6) or React. **Default: Streamlit**, switch to React only if all Day 6 work is done by midday.

4. **A2A Agent Card:** include or skip. **Default: include** as a Day 5 stretch — small code, completes the narrative.

5. **Multiple policies in the demo:** one deep (Smith on Molina) or three breadth (Molina + MassHealth + OHA). **Default: one deep**, mention generalization in the deck.

6. **Policy hot-reload:** restart-required or watch filesystem. **Default: restart-required** for prototype.

7. **Policy versioning:** separate files (`molina-mcp-032-v1.json`) or in-file date ranges. **Default: separate files**, simpler.

---

## 13. Acceptance Criteria (the "done" checklist)

Before declaring the prototype complete, verify:

**Foundation:**
- [ ] `docker compose up` brings up working API
- [ ] Health check returns 200
- [ ] OpenAPI docs accessible at `/docs`

**Policy authoring:**
- [ ] `policies/molina-mcp-032.json` exists with ~30 leaves
- [ ] Every criterion has verbatim policy_citation
- [ ] Policy validates against schema at load
- [ ] Citation faithfulness check passes 100% on policy citations

**Document ingestion:**
- [ ] Smith PDF parses with PyMuPDF
- [ ] Sections detected correctly (Fax Cover, H&P, PT eval, etc.)
- [ ] Each section has page + offset metadata

**Intake Agent:**
- [ ] Smith case produces FHIR Patient with DOB, name, MRN
- [ ] Conditions extracted: M54.16, M79.18, M47.816
- [ ] Observation NRS 9/10 extracted
- [ ] MedicationRequests: ibuprofen, acetaminophen
- [ ] ServiceRequest: CPT 62323, lumbar interlaminar ESI
- [ ] Every entity has verifiable citations (100% pass)

**Policy Selection:**
- [ ] Smith case → ok → molina-mcp-032, branch=initial
- [ ] Level 1 eval ≥14/15 pass
- [ ] False auto-pick rate = 0%

**Adjudicator:**
- [ ] All ~30 criteria evaluated in parallel
- [ ] Each verdict has patient_evidence with verifiable quotes
- [ ] Level 2 eval ≥27/30 pass
- [ ] Smith case's conservative_therapy = unclear (or not_met)
- [ ] Smith case's myofascial exclusion = unclear

**Determination:**
- [ ] Smith case root = unclear → pend
- [ ] Two specific missing-info requests match expected wording

**PAS Bundle:**
- [ ] Valid Da Vinci PAS Bundle generated
- [ ] ClaimResponse.outcome maps correctly (queued/complete/partial/error)
- [ ] processNote includes per-criterion rationale

**MCP server:**
- [ ] Server reachable at `/mcp`
- [ ] Tools callable from Claude Desktop or test client
- [ ] Same Smith case reproducible via MCP `evaluate_prior_auth` call

**UI:**
- [ ] Case List shows Smith case with correct status badge
- [ ] Case Detail shows all three panels (PDF / FHIR / criteria)
- [ ] Click FHIR fact → PDF highlights source
- [ ] Click criterion → expansion shows evidence + reasoning + policy quote
- [ ] Action buttons present and functional

**Deployment:**
- [ ] Fly.io URL public and working
- [ ] Demo video records full Smith walkthrough
- [ ] README has setup instructions

**Deck:**
- [ ] 8 slides max
- [ ] Architecture diagram
- [ ] Smith case walkthrough screenshot
- [ ] Eval results table
- [ ] Production roadmap slide
- [ ] Demo link

---

## Closing Note for the Builder

The success of this prototype depends most on three things:

1. **The policy criteria tree.** Author it carefully. It is the spec everything else evaluates against. Spend a real four hours on it. Re-read the Molina PDF twice while authoring. Verify every quote.

2. **Citation faithfulness.** Do not relax the substring verification contract. Every quote, both policy-side and patient-side, must be verifiable. This is the trust statement of the entire system.

3. **The Smith case.** It is the worked example for a reason — it is genuinely ambiguous in a way that demonstrates the system's value. A naive system gets it wrong; a well-designed system identifies the gaps with specific actionable info requests. Make sure the Smith case produces the right pend determination with the right two info requests. If it does, the demo works.

Everything else is engineering. The above three points are where the system either earns trust or doesn't.

Build well.
