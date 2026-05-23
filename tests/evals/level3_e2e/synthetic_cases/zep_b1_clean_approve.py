"""L3-ZEP-B1 — Clean approve case for MassHealth Zepbound policy.

Angela Davis, 45F, MassHealth Medicaid. Phentermine step-therapy completed,
documented plateau response, BMI 34.1 with comorbidities. Requesting Zepbound.
Expected: approve. NOT the Taylor case.
"""

from tests.evals.level3_e2e.synthetic_bundle import L3SyntheticCase


CASE = L3SyntheticCase(
    id="L3-ZEP-B1",
    domain="zepbound",

    family="Davis",
    given="Angela",
    birth_date="1980-07-14",
    gender="female",
    state="MA",
    mrn="MRN-ZEP-B1",

    payer_id="masshealth",
    payer_display="Commonwealth of Massachusetts MassHealth",
    lob="Medicaid",
    member_id="WS-B1-007",

    cpt_code="tirzepatide-zepbound",
    service_display="Tirzepatide injection (Zepbound) 5 mg/0.5 mL, 4 pens per 28 days for obesity with type 2 diabetes",
    request_category="pharmacy",
    icd10_codes=["E66.01", "E11.9", "I10"],
    service_date="2026-04-08",

    expected_outcome="approve",
    expected_info_keywords=[],
    expected_deny_keywords=[],

    notes="Clean Zepbound approve: BMI 34.1, T2DM + HTN comorbidities, completed adherent phentermine trial with documented plateau, all baseline documentation in order. Different patient than Taylor (Taylor is on Zepbound already without documented phentermine trial).",

    narrative="""\
CHIEF COMPLAINT
Class I obesity with type 2 diabetes mellitus and hypertension. Requesting
tirzepatide (Zepbound) following failure of phentermine therapy.

HISTORY OF PRESENT ILLNESS
Angela Davis is a 45-year-old female with adult-onset obesity, currently
BMI 34.1, with comorbid type 2 diabetes mellitus and hypertension. Her
weight has trended upward since pregnancy in her late 20s. She was started
on phentermine 37.5 mg daily on 2025-11-15 after weight loss counseling
and dietary review. Initial response was modest (3 kg loss in first month),
but weight has been static for the past four months despite continued
maximally tolerated phentermine and ongoing lifestyle intervention.

Current motivation is high. She walks 30 minutes most days and is enrolled
in a Mediterranean-diet nutrition program. She is not interested in
bariatric surgery at this time.

PAST MEDICAL HISTORY
- Obesity, class I (E66.01)
- Type 2 diabetes mellitus, A1c 7.4% on metformin
- Essential hypertension, controlled on lisinopril
- Hyperlipidemia, controlled on rosuvastatin
- Mild obstructive sleep apnea (AHI 8) — uses CPAP

CURRENT MEDICATIONS
- metformin 1000 mg orally twice daily for type 2 diabetes
- lisinopril 20 mg orally daily for hypertension
- rosuvastatin 20 mg orally daily for hyperlipidemia
- phentermine 37.5 mg orally daily (started 2025-11-15, ongoing) — for weight
- daily multivitamin

No other GLP-1 receptor agonists prescribed (no semaglutide, no liraglutide,
no dulaglutide, no exenatide). The patient is NOT on Mounjaro currently.

ALLERGIES
No known drug allergies.

WEIGHT AND BMI HISTORY (LOINC 29463-7 weight, 39156-5 BMI)
- 2024-04-10: weight 92.0 kg, BMI 33.6 (baseline pre-intervention)
- 2025-10-30: weight 95.5 kg, BMI 34.9 (initial phentermine evaluation)
- 2025-11-15: weight 95.5 kg, BMI 34.9 (phentermine started)
- 2025-12-15: weight 93.2 kg, BMI 34.1 (1-month phentermine, 2.3 kg loss)
- 2026-01-15: weight 92.4 kg, BMI 33.8 (2-month phentermine, 3.1 kg total)
- 2026-02-15: weight 92.4 kg, BMI 33.8 (3-month phentermine, no further loss)
- 2026-03-15: weight 92.6 kg, BMI 33.9 (4-month phentermine, no further loss)
- 2026-04-01: weight 92.5 kg, BMI 33.8 (current, on phentermine 5 months)

Net weight reduction from phentermine baseline (2025-11-15): 3.0 kg (3.1%).
Net weight loss over the last 3 months on max-tolerated dose: <0.5 kg
(within measurement noise) — documented plateau.

PHARMACY CLAIMS / ADHERENCE
Member's MassHealth pharmacy claims show phentermine fills:
- 2025-11-15: 30-day supply
- 2025-12-15: 30-day supply
- 2026-01-15: 30-day supply
- 2026-02-15: 30-day supply
Total days covered in last 120 days: 120 of 120 (>= 90 days threshold; fully
adherent).

COUNSELING DOCUMENTATION
At every clinical visit (monthly throughout phentermine trial), the member
has been counseled on:
- Reduced-calorie diet (Mediterranean style, ~1500 kcal/day target)
- Increased physical activity (150 minutes moderate-intensity per week)

ASSESSMENT AND PLAN
45yo female with class I obesity (BMI 33.8), type 2 diabetes mellitus,
hypertension, and mild OSA who has completed a 5-month adherent trial of
maximally tolerated phentermine with documented plateau (no weight loss in
the last 3 months despite continued therapy). She has been continuously
counseled on diet and exercise. She does not have any contraindication to
GLP-1 or GIP/GLP-1 agonists.

Plan: Initiate tirzepatide (Zepbound) 5 mg/0.5 mL subcutaneous pen injector,
1 pen weekly (4 pens per 28 days). Will continue current phentermine for
2-week overlap then discontinue. Will continue metformin, lisinopril,
rosuvastatin. Will continue diet/exercise counseling at monthly follow-up.

Medical necessity for Zepbound over Mounjaro: although member has T2DM, her
A1c is already at target (7.4%) on metformin, so the diabetes management
indication does not apply. Zepbound is being requested specifically for
weight management in obesity with comorbidities. Mounjaro is not appropriate
because it is FDA-indicated for diabetes glycemic control rather than chronic
weight management, and the member does not require additional diabetes
agents at this time.

Quantity: 4 pens per 28 days (within the policy's maximum).
GLP-1 polypharmacy: not applicable (no other GLP-1 currently prescribed).
""",
)
