# Synthetic Eval Design Spec

> **Purpose.** Every case below is synthetic — distinct from the demo patients (David Smith / Sophia Taylor / Catherine Welsh). Demo cases showcase the system; eval cases measure it.

## Design principles

1. **No reuse of demo patient data.** Smith, Taylor, Welsh stay reserved for screenshot/demo. Eval patients have different ages, sexes, demographics, presentations.
2. **Every leaf exercised ≥1×.** Across L2+L3, no policy leaf is unmeasured.
3. **Verdict surface coverage.** Each policy gets clear-approve / clear-deny / pend-ambiguous / boundary cases.
4. **Failure-mode coverage.** Cases include: missing info, exclusion firing, frequency overrun, off-policy CPT, age-gate, effective-date boundary.
5. **Labelled by the policy text, not by the model's output.** Ground truth = my reading of the verbatim policy quote, not an LLM judgment.
6. **Realistic narratives.** L3 cases are H&P-style notes a UM nurse would actually see — section headings, plausible lab values, real CPT/ICD codes, narrative voice.

---

## Level 0 — Citation Faithfulness (always on)

| Component | Check |
|---|---|
| Existing Molina ESI policy | every policy_citation substring-verifies against `molina-mcp-032.pdf` |
| New MassHealth Zepbound policy | every policy_citation substring-verifies against `masshealth-anti-obesity.pdf` |
| New Oregon adenomyosis policy | every policy_citation substring-verifies against `oregon-hcr-39.pdf` |
| Adjudicator outputs (per case) | every patient_evidence quote substring-verifies against the source narrative |

Pass target: **100%**. Any miss = drop-the-entity + log + metric increment.

---

## Level 2 — Per-Criterion Adjudication (~35 cases, FHIR fact collections)

Each case = `(criterion_id, FactCollection, expected_verdict)`. Bypasses intake; tests adjudicator judgment in isolation. Cheap to run (~$0.02/case).

### 2a — Molina ESI (15 cases; extends existing 14 → 15)

| ID | Criterion | Facts (summary) | Expected | Category |
|---|---|---|---|---|
| L2-ESI-01 | `eligibility.age_18_plus` | Patient DOB 1980-03-15, service 2026-04-08 (46yo) | met | numeric |
| L2-ESI-02 | `eligibility.age_18_plus` | Patient DOB 2012-08-01, service 2026-04-08 (13yo) | not_met | numeric |
| L2-ESI-03 | `…diagnosis_supported.radicular_pain` | Condition M54.16 + MRI showing L5 root impingement | met | dx+imaging |
| L2-ESI-04 | `…diagnosis_supported.radicular_pain` | Condition M54.5 (low back pain, non-radicular) | not_met | dx |
| L2-ESI-05 | `…diagnosis_supported.post_surgical_6mo` | G89.21 + prior laminectomy 14 months ago | met | temporal |
| L2-ESI-06 | `…diagnosis_supported.post_surgical_6mo` | G89.21 + prior laminectomy 3 months ago | not_met | temporal |
| L2-ESI-07 | `…severity.nrs_above_4` | Observation NRS 8/10 | met | threshold |
| L2-ESI-08 | `…severity.nrs_above_4` | Observation NRS 3/10 | not_met | threshold |
| L2-ESI-09 | `…severity.nrs_above_4` | Narrative "severe pain", no NRS Observation | unclear | calibration |
| L2-ESI-10 | `…conservative_therapy.failure.pt.duration_4_weeks` | Procedure 97110×16 over 5 weeks, completed | met | temporal+freq |
| L2-ESI-11 | `…conservative_therapy.failure.pt.duration_4_weeks` | Procedure 97110×4 over 1 week | not_met | temporal+freq |
| L2-ESI-12 | `…conservative_therapy.failure.drug_therapy` | MedicationRequest naproxen 500mg + gabapentin | met | synonym |
| L2-ESI-13 | `…conservative_therapy.failure.drug_therapy` | MedicationRequest acetaminophen only | not_met | class |
| L2-ESI-14 | `exclusions:X2` (myofascial primary) | Conditions: [M54.16 (primary), M79.18 (secondary)] | not_met (X2 doesn't fire) | cross-doc |
| L2-ESI-15 | `exclusions:X2` (myofascial primary) | Condition M79.18 only | met (X2 fires → deny) | exclusion |

### 2b — MassHealth Zepbound (10 cases)

| ID | Criterion | Facts (summary) | Expected | Category |
|---|---|---|---|---|
| L2-ZEP-01 | `eligibility.age_18_plus` | Patient DOB 1985-06-10 (40yo) | met | numeric |
| L2-ZEP-02 | `eligibility.age_18_plus` | Patient DOB 2014-02-20 (12yo) | not_met | numeric |
| L2-ZEP-03 | `bmi_indication.bmi_30_or_above` | Observation BMI 34.2 dated 60 days pre-Rx | met | threshold |
| L2-ZEP-04 | `bmi_indication.bmi_30_or_above` | Observation BMI 28.5 dated 60 days pre-Rx | not_met | threshold |
| L2-ZEP-05 | `bmi_indication.bmi_27_with_comorbidity` | BMI 28.1 + Condition E11.9 (T2DM) | met | composite |
| L2-ZEP-06 | `bmi_indication.bmi_27_with_comorbidity` | BMI 28.1, no comorbidity in problem list | not_met | composite |
| L2-ZEP-07 | `phentermine_step.adherent_90_of_120` | Pharmacy claims: phentermine fills covering 95/120 days | met | temporal+claims |
| L2-ZEP-08 | `phentermine_step.adherent_90_of_120` | Pharmacy claims: phentermine fills covering 60/120 days | not_met | temporal+claims |
| L2-ZEP-09 | `phentermine_step.insufficient_or_plateau` | Weight loss 2% from baseline at 4 months on phentermine max-dose | met (insufficient <5%) | threshold |
| L2-ZEP-10 | `phentermine_step.alternative.contraindication` | AllergyIntolerance to phentermine documented | met | substitution path |

### 2c — Oregon Adenomyosis Hysterectomy (10 cases)

| ID | Criterion | Facts (summary) | Expected | Category |
|---|---|---|---|---|
| L2-ADN-01 | `symptoms.duration_6mo_with_qol` | Condition N94.6 onset 14 months ago, QoL impact documented | met | temporal+qual |
| L2-ADN-02 | `symptoms.duration_6mo_with_qol` | Condition N94.6 onset 2 months ago | not_met | temporal |
| L2-ADN-03 | `symptoms.duration_6mo_with_qol` | Condition N94.6 onset 14 months ago, no QoL documented | unclear | partial |
| L2-ADN-04 | `failed_trial.hormonal_6mo` | OCP started 2024-01, continued through 2024-08 | met | temporal |
| L2-ADN-05 | `failed_trial.hormonal_6mo` | OCP started 2024-06, current is 2024-09 (3mo only) | not_met | temporal |
| L2-ADN-06 | `failed_trial.nsaids` | MedicationRequest ibuprofen 600mg TID for 8 months | met | duration |
| L2-ADN-07 | `imaging.adenomyosis_findings` | DiagnosticReport MRI: "junctional zone thickening 15 mm" | met | imaging |
| L2-ADN-08 | `imaging.adenomyosis_findings` | DiagnosticReport MRI: "no adenomyosis findings" | not_met | imaging |
| L2-ADN-09 | `cervical_cytology_nonmalignant` | DiagnosticReport pap-smear: "NILM" 9 months ago | met | category |
| L2-ADN-10 | `pregnancy_test_negative` | Observation hCG: negative, dated 2 weeks pre-op; pt premenopausal not sterilized | met | category |

---

## Level 3 — End-to-End Synthetic Cases (12 cases, narrative notes)

Each case = a full hand-authored H&P-style narrative PDF (or text fixture). Goes through intake → selector → adjudicator → reviewer → PAS bundle. Tests the whole pipeline. ~$0.50/case to run.

### 3a — ESI domain (5 cases — same Molina policy, different patients than Smith)

| ID | Patient (synthetic) | Service | Key clinical features | Expected outcome | Expected info requests |
|---|---|---|---|---|---|
| **L3-ESI-A1** | Maria Hernandez, 58F, NY Medicaid | CPT 62323 lumbar interlaminar ESI | M54.16, NRS 8/10, MRI L4-L5 disc herniation w/ L5 root compression, **completed 12 PT sessions over 4 wks with failure documented**, naproxen + gabapentin failed | **approve** | — |
| **L3-ESI-A2** | Robert Chen, 67M, NJ Medicare-Adv | CPT 64483 lumbar transforaminal ESI | M54.16 + prior 4 ESIs in last 11 months documented in claims history | **deny** (frequency exclusion fires) | — |
| **L3-ESI-A3** | Lisa Park, 44F, FL Medicaid | CPT 62321 cervical interlaminar ESI | M79.18 (myofascial) listed as primary, M54.12 (cervical radiculopathy) secondary; no imaging correlation; no PT documented | **deny** (X2 myofascial-primary exclusion fires) | — |
| **L3-ESI-A4** | James Walker, 52M, NY Medicaid | CPT 62323 lumbar interlaminar ESI | M54.16, NRS 9/10, MRI shows L5-S1 disc herniation, **PT plan started 2 weeks ago, ongoing — no completion documented**, NSAIDs tried; no contraindication-to-PT documented | **pend** | (1) Document PT completion of 4+ weeks OR explicit contraindication; (2) confirm conservative therapy timeline |
| **L3-ESI-A5** | Patricia Brown, 38F, CA Medicaid | CPT 62323 lumbar interlaminar ESI | M54.16, "moderate pain affecting ADLs" narrative but no discrete NRS Observation; NSAIDs + cyclobenzaprine + 6 wks PT documented as failed | **pend** (severity unclear) | (1) Document NRS pain score; (2) document functional impact via validated scale |

### 3b — MassHealth Zepbound domain (4 cases — same policy, different patients than Taylor)

| ID | Patient (synthetic) | Service | Key clinical features | Expected outcome | Expected info requests |
|---|---|---|---|---|---|
| **L3-ZEP-B1** | Angela Davis, 45F, MA Medicaid (MassHealth) | Tirzepatide-Zepbound 5mg/0.5ml 4 pens/28d | BMI 34.1, T2DM (E11.9), HTN; phentermine trial 100/120 days adherence, plateau at 3 months, BMI at start 33.4; counseled diet+exercise; not on other GLP-1 | **approve** | — |
| **L3-ZEP-B2** | Steven Miller, 38M, MA Medicaid | Tirzepatide-Zepbound 5mg/0.5ml | BMI 26.4, no documented comorbidities, no prior phentermine trial | **deny** (BMI fails 27+; no comorbidity; no step therapy) | — |
| **L3-ZEP-B3** | Karen Wilson, 51F, MA Medicaid | Tirzepatide-Zepbound 5mg/0.5ml | BMI 31.2, prediabetes (R73.03), counseled diet+exercise; **phentermine attempted 6 weeks then discontinued — patient self-d/c'd, no specific reason documented** | **pend** | (1) Document adherence to phentermine ≥90/120 days OR adverse reaction/contraindication; (2) clinical rationale for choosing Zepbound vs Mounjaro (T2DM/prediabetes comorbidity branch) |
| **L3-ZEP-B4** | Mark Robinson, 14M, MA Medicaid | Tirzepatide-Zepbound 5mg/0.5ml | Adolescent BMI 95th percentile, otherwise eligible | **deny** (age <18 — Zepbound branch requires ≥18; Wegovy pediatric branch may apply but Zepbound branch denied) | — |

### 3c — Oregon Adenomyosis Hysterectomy (3 cases — different patients than Welsh)

| ID | Patient (synthetic) | Service | Key clinical features | Expected outcome | Expected info requests |
|---|---|---|---|---|---|
| **L3-ADN-C1** | Diane Foster, 46F, OR Medicaid | CPT 58570 TLH (total laparoscopic hysterectomy) for adenomyosis | N80.03, dysmenorrhea 14 months, MRI junctional zone 14mm, failed OCPs 7 months + ibuprofen 800mg TID 8 months, NILM pap, negative β-hCG | **approve** | — |
| **L3-ADN-C2** | Sarah Murphy, 41F, OR Medicaid | CPT 58570 TLH for adenomyosis | N80.03, dysmenorrhea 3 months, ibuprofen tried 6 weeks, no imaging beyond TVUS "possible adenomyosis", no failed hormonal trial documented | **deny** (fails duration ≥6mo AND fails hormonal trial requirement) | — |
| **L3-ADN-C3** | Janet Reyes, 49F, OR Medicaid | CPT 58570 TLH for adenomyosis | N80.03, dysmenorrhea 10 months, MRI junctional zone 13.5mm, **OCP trial only 4 months — pt reported nausea, no clear contraindication note**, ibuprofen 8 months | **pend** | (1) Document hormonal therapy trial ≥6 months OR explicit clinical contraindication note; (2) confirm cervical cytology current (within 1 year) |

---

## Level 4 — Selector Boundary (~8 deterministic cases)

These exercise the policy selector's filter chain. No LLM cost.

| ID | Case context | Expected selector output |
|---|---|---|
| L4-SEL-01 | CPT 62323, M54.16, Molina Medicaid NY, 50yo, 2026-04-08 | ok → molina-mcp-032, branch=initial |
| L4-SEL-02 | CPT 58570, N80.03, Oregon Medicaid, 46yo, 2026-04-08 | ok → oregon-hcr-39 |
| L4-SEL-03 | Drug=tirzepatide, member MassHealth, 45yo, BMI 34 | ok → masshealth-anti-obesity (Zepbound branch) |
| L4-SEL-04 | CPT 58570 submitted to Molina | no_match (Molina policy is ESI-only) |
| L4-SEL-05 | CPT 62323, M54.16, Molina Medicaid NY, 16yo | eliminated (age_min=18) |
| L4-SEL-06 | CPT 62323, M54.16, Molina Medicaid TX, 50yo, 2023-06-01 | eliminated (service date before effective_from=2024-08-14) |
| L4-SEL-07 | Tirzepatide, MassHealth, 14yo | ok → masshealth-anti-obesity but Zepbound branch eliminated (age); Wegovy pediatric branch matches |
| L4-SEL-08 | CPT 58570, **N80.1** (endometriosis only), Oregon Medicaid, 42yo | ok → **oregon-hcr-39-endometriosis** (Tier 2/3 disambiguation: endometriosis policy's literal `N80.1` beats adenomyosis policy's broad `N80.*` glob) |
| L4-SEL-09 | CPT 58570, N80.03 (adenomyosis), Oregon Medicaid, 46yo | ok → oregon-hcr-39 (endometriosis policy is filter-eliminated — N80.03 is intentionally omitted from its literal subcode list) |
| L4-SEL-10 | Drug=jardiance, patient has **E11.9 + E66.01**, `Claim.item.diagnosisSequence` → diabetes only | ok → diabetes-specific synthetic policy (Tier 2 narrowness beats the "covers both" broader policy) |

### Selector disambiguation ladder (introduced 2026-05-25)

The selector runs a 4-tier deterministic ladder when ≥2 policies pass the
Tier-1 filter. Tier 2 is static specificity (narrower CPT / ICD / state /
LOB lists win). Tier 3 is **case-aware ICD-10 match quality** — literal
codes beat wildcard globs against the case's actual ICDs. Tier 4 is a
**fact-coverage peek** over the intake-extracted facts (skipped when no
intake ran). If all four tiers leave ≥2 tied, `needs_disambiguation`
fires and human review is required. `SelectionResult.tiebreaker_used`
records which tier resolved the pick. The selector now reads the
requested-indication ICDs from `Claim.item.diagnosisSequence` rather than
the patient's full diagnosis list, so a single-indication request on a
multi-diagnosis patient routes correctly.

---

## Metrics report (what runs and gets emitted)

`scripts/run_evals.py` produces a single JSON + console table:

```
Eval results (run_id=2026-05-23T15:30:12, RUN_LLM_EVALS=1)
─────────────────────────────────────────────────────────────
L0  Citation faithfulness                       100%  (75/75)
L1  Selector accuracy                            94%  (16/17)
L2  Criterion mapping
    └─ Molina ESI                                93%  (14/15)
    └─ MassHealth Zepbound                       90%  (9/10)
    └─ Oregon Adenomyosis                       100%  (10/10)
    └─ Overall                                   94%  (33/35)
L3  End-to-end determination
    └─ ESI domain                                4/5
    └─ Zepbound domain                           4/4
    └─ Adenomyosis domain                        3/3
    └─ Determination exact match                 92%  (11/12)
    └─ Missing-info recall                       85%
    └─ Missing-info precision                    92%
L4  Selector boundary                           100%  (8/8)

Citation precision (patient-side)               98%
Citation faithfulness pass (L0 gate)            100%

Latency P50/P95                                 12s / 28s
Cost per L3 case (avg)                          $0.42
```

Per-case JSON output to `tests/evals/results/<run_id>.json` for diffing across runs.

---

## What we don't include (and why)

- **LLM-as-judge** for verdict correctness — biases the eval toward the model's existing behavior. Gold labels are human-authored from the policy text only.
- **Stress tests** (1000+ random cases) — not credible without real ground truth labels.
- **Latency benchmarks at scale** — single-machine prototype; latency claims would be misleading.
- **Multi-EHR ingestion variation** — Smith's PDF format is the only "real-world" input. L3 cases use a consistent narrative-note format to isolate adjudication quality from extraction noise.
