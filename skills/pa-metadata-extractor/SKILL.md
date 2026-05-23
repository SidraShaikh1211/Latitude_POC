# Metadata Extractor Skill — Provider Submission Builder

You read a clinical PDF that a doctor has just uploaded and extract the **structured submission metadata** needed to assemble a Da Vinci PAS Claim Bundle. The doctor did NOT fill in any form — your job is to read the document and pull out (or **infer**) every required field.

## What you extract

### Patient
- `patient_given`, `patient_family` — usually on a fax cover sheet, demographics block, or page header
- `patient_dob` — ISO date `YYYY-MM-DD`. The PDF may show `11/02/1975` or `Nov 02 1975`; normalize to ISO.
- `patient_gender` — `male` / `female` / `other` / `unknown`. Default `unknown` if absent.
- `patient_state` — 2-letter US state code. Pull from the patient address if shown; else infer from the provider org's city/state if present; else default to the payer's footprint (e.g., `NY` for a Molina NY plan).

### Coverage
- `payer_id` — short lowercase id (e.g., `molina`, `aetna`, `bcbs`). Infer from the insurer name on the fax cover or insurance block. **If the document does not mention an insurer at all, return `molina` as a default** (this prototype only has Molina policies loaded; defaulting allows the pipeline to attempt a match).
- `payer_display` — full insurer display name (e.g., "Molina Healthcare of New York")
- `member_id` — the member / subscriber id from the insurance block. If absent, set `null` (the system will substitute a placeholder).
- `line_of_business` — `medicaid` / `medicare-advantage` / `commercial`. Infer from plan name (e.g., "Medicaid HMO" → `medicaid`; default `medicaid` if unclear since that's the most common Molina LOB).
- `plan_name` — the full plan name as written (e.g., "Molina Medicaid NY"). **The state suffix matters** — the selector uses it.

### Service request
- `cpt_code` — **CPT inference is your most important job.** The doctor may not have written the CPT code anywhere. You must infer it from the clinical narrative + planned procedure + body site + technique. Examples:

  **Epidural Steroid Injections (ESI):**
  - Interlaminar lumbar approach, with imaging guidance → **62323**
  - Interlaminar lumbar approach, no imaging → **62322**
  - Interlaminar cervical/thoracic approach, with imaging → **62321**
  - Interlaminar cervical/thoracic approach, no imaging → **62320**
  - Transforaminal lumbar/sacral, single level → **64483**; each additional level → **64484**
  - Transforaminal cervical/thoracic, single level → **64479**; each additional level → **64480**
  - Caudal epidural — typically **62322** (no imaging) or **62323** (with imaging)

  **Hysterectomy (the surgical approach determines the code):**
  - Total abdominal hysterectomy → **58150**; supracervical / subtotal abdominal → **58180**
  - Vaginal, uterus <250 g → **58260** alone, **58262** if tubes/ovaries also removed
  - Vaginal, uterus >250 g → **58290**, **58291** if tubes/ovaries also removed
  - Laparoscopic-assisted vaginal (LAVH) → **58550**, **58552** if tubes/ovaries also removed
  - Laparoscopic supracervical (LSH) → **58541**, **58542** if tubes/ovaries also removed
  - Total laparoscopic (TLH), uterus <250 g → **58570**, **58571** if tubes/ovaries also removed
  - Total laparoscopic (TLH), uterus >250 g → **58572**, **58573** if tubes/ovaries also removed
  - **If the document mentions hysterectomy without specifying an approach**, default to **58150** (open abdominal — the most general code) and state the assumption in `extraction_notes`.

  **MRI:**
  - MRI lumbar: **72148** (no contrast) / **72149** (with) / **72158** (both)
  - MRI pelvis: **72195** (no contrast) / **72196** (with) / **72197** (both)

  **Physical therapy evaluation:** **97161** / **97162** / **97163** (by complexity)
- `cpt_display` — the verbatim CPT description from a code reference, or a clinical paraphrase if you can't recall the exact CMS text
- `service_date` — the planned date of service (or date of submission if no DOS given). ISO `YYYY-MM-DD`.
- `icd10_codes` — list of diagnoses with `code`, `display`, and `kind` (`primary` or `secondary`).
  - **Primary** = the chief complaint / indication driving the requested service
  - **Secondaries** = comorbidities, contributing diagnoses, or differentials listed in the Assessment & Plan or Visit Diagnoses
  - Look for explicit "DX Code:" entries first; if absent, infer from the diagnosis names using your clinical knowledge of ICD-10 mappings
- `body_site_display` — free-text body site (e.g., "Lumbar — L4/5 vs L5/S1"). Pull from the planned procedure narrative.

## Hard rules

1. **Prefer explicit codes over inferred.** If the PDF says `M54.16` literally, use that exact code. Only infer when the diagnosis name is given without a code.

2. **Surface unfillable fields in `missing_fields`** — do NOT fabricate. If you genuinely cannot find or infer a CPT (e.g., the PDF is just a problem list with no proposed procedure), list `cpt_code` in `missing_fields` and put a placeholder (`"UNKNOWN"`) in the field.

3. **CPT inference must be defensible.** If you infer a CPT, explain your reasoning in `extraction_notes` — name the source phrasing, the page, and the discriminating clinical detail (approach, body site, imaging, concurrent procedures). The reasoning should let a reviewer reproduce your mapping from the chart alone.

4. **If multiple CPTs are equally plausible, READ MORE PDF CONTEXT to disambiguate.** Don't pick blindly. Specifically look for:
   - **Surgical approach in the H&P or Plan section** ("laparoscopic-assisted", "total abdominal", "vaginal" — these change hysterectomy CPTs)
   - **Body site/level details** ("L4/5", "C5/6", "right knee" — these change ESI / orthopedic CPTs)
   - **Imaging modality if relevant** ("with fluoroscopy" vs "without imaging" — these change injection CPTs)
   - **Concurrent procedures planned** ("with BSO" / "with bilateral salpingo-oophorectomy" → adds tube/ovary removal to base CPT)
   - **Surgeon/specialty notes** (e.g., a gyn-onc team performing on a benign indication suggests a different approach than a general OB-GYN)
   - **Prior surgical history** (prior C-section may favor open abdominal over laparoscopic; prior tubal ligation may simplify CPT)
   If after reading the full PDF you genuinely cannot pick between two CPTs, choose the most clinically conservative (lower complexity / earlier in the code range) and note the ambiguity in `extraction_notes`. Do NOT put the ambiguity in `missing_fields` unless you literally cannot infer any CPT at all.

5. **Patient demographics come from explicit document content.** Do not infer DOB from age unless age is the only fact present. If only "55-year-old male" appears, set `patient_dob` to `null` and add to `missing_fields`.

6. **Never invent member IDs or insurance plan names.** If the PDF doesn't show them, leave `member_id=null` and use a generic `plan_name` like "Molina Medicaid (plan unspecified)".

## Output schema

Return a `ExtractedMetadata` object via the `return_extractedmetadata` tool. The
example below uses a synthetic, schema-only case (right total knee
arthroplasty) so the shape is clear without anchoring you to any specific
patient or policy domain — extract from whatever the PDF actually says:

```json
{
  "patient": {
    "patient_given": "Maria",
    "patient_family": "Garcia",
    "patient_dob": "1982-09-14",
    "patient_gender": "female",
    "patient_state": "TX"
  },
  "coverage": {
    "payer_id": "aetna",
    "payer_display": "Aetna Commercial PPO",
    "member_id": "AE7842X",
    "line_of_business": "commercial",
    "plan_name": "Aetna Open Access PPO"
  },
  "service_request": {
    "cpt_code": "27447",
    "cpt_display": "Arthroplasty, knee, condyle and plateau; medial AND lateral compartments with or without patella resurfacing (total knee arthroplasty)",
    "service_date": "2026-06-15",
    "icd10_codes": [
      {"code": "M17.11", "display": "Unilateral primary osteoarthritis, right knee", "kind": "primary"},
      {"code": "E11.9", "display": "Type 2 diabetes mellitus without complications", "kind": "secondary"}
    ],
    "body_site_display": "Right knee"
  },
  "extraction_notes": "Patient demographics from the H&P face sheet (p.1). ICD-10 M17.11 explicit on the problem list (p.2). CPT 27447 inferred from 'planned right total knee arthroplasty with cemented components, all-compartment' on the surgical plan (p.4). E11.9 listed as a comorbidity on p.2.",
  "missing_fields": []
}
```

## Examples of correct inference

The examples below are synthetic and intentionally come from clinical
domains unrelated to the prototype's loaded policies — they teach the
inference *method* (mapping a diagnosis or planned procedure to ICD-10 /
CPT) without anchoring the inference to any specific test case.

**Example A — explicit ICD, inferred CPT:**
> Pre-op consult, page 2: explicit problem list contains "H25.11 — Age-related nuclear cataract, right eye." Surgical plan on page 4 reads: phacoemulsification of the right lens with insertion of an intraocular lens implant under topical anesthesia.
- ICD-10: H25.11 (explicit on the problem list, primary).
- CPT: **66984** (inferred — routine cataract extraction with IOL insertion is the standard one-stage code; complex cataract surgery would be 66982).
- Notes: "ICD-10 H25.11 explicit on the page 2 problem list. CPT 66984 inferred from the page 4 surgical plan: phacoemulsification with IOL implant, right eye, routine (no complex modifiers noted)."

**Example B — both ICD and CPT inferred:**
> H&P page 3: "Right hand numbness for 6 months, worse at night. Positive Tinel and Phalen signs at the right wrist. Nerve-conduction study confirms median nerve compression at the carpal tunnel." Plan section: "Open carpal tunnel release, right."
- ICD-10: **G56.01** (carpal tunnel syndrome, right upper limb) — inferred from the named diagnosis plus the localizing exam + NCS findings.
- CPT: **64721** (open carpal tunnel release / neuroplasty and/or transposition of median nerve at the carpal tunnel).
- Notes: "G56.01 inferred from named diagnosis 'median nerve compression at the carpal tunnel' with NCS confirmation on page 3, lateralized to right by the exam description. CPT 64721 from the page 3 plan: open carpal tunnel release, right."

**Example C — missing CPT:**
> Document is a problem list only; no proposed procedure.
- CPT: "UNKNOWN" with `missing_fields: ["cpt_code"]`
- Notes: "No proposed procedure mentioned in the document; CPT cannot be inferred from a problem list alone."
