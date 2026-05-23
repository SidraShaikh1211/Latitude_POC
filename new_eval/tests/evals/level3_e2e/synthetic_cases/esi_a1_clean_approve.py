"""L3-ESI-A1 — Clean approve case for Molina ESI policy.

Maria Hernandez, 58F, Molina Medicaid NY. Clean radiculopathy presentation
with imaging correlation AND completed conservative therapy. Expected:
approve. NOT the Smith case.
"""

from tests.evals.level3_e2e.synthetic_bundle import L3SyntheticCase


CASE = L3SyntheticCase(
    id="L3-ESI-A1",
    domain="esi",

    family="Hernandez",
    given="Maria",
    birth_date="1968-03-22",
    gender="female",
    state="NY",
    mrn="MRN-ESI-A1",

    payer_id="molina",
    payer_display="Molina Healthcare of New York",
    lob="Medicaid",
    member_id="M-A1-001",

    cpt_code="62323",
    service_display="Injection(s), of diagnostic or therapeutic substance(s), interlaminar epidural, lumbar or sacral (caudal); with imaging guidance (e.g., fluoroscopy or CT)",
    request_category="procedural",
    icd10_codes=["M54.16"],
    service_date="2026-04-08",

    expected_outcome="approve",
    expected_info_keywords=[],
    expected_deny_keywords=[],

    notes="Clean approve: imaging-correlated radicular pain + completed PT + multi-drug conservative therapy. Different patient than Smith.",

    narrative="""\
CHIEF COMPLAINT
Right-sided low back pain with right L5 radiculopathy, requesting lumbar
interlaminar epidural steroid injection.

HISTORY OF PRESENT ILLNESS
Maria Hernandez is a 58-year-old female with an 8-month history of progressive
right-sided low back pain. Pain radiates down the posterolateral right thigh,
across the lateral calf, and into the dorsum of the right foot in an L5
dermatomal distribution. She describes the pain as sharp and shooting,
exacerbated by sitting, standing, and walking more than one block. Lying
supine partially relieves the pain. She reports paresthesia along the L5
dermatome and intermittent foot drop sensation.

Pain has progressively worsened despite a documented and completed course of
conservative therapy (see below). At today's visit she rates pain at 8 out of
10 on the Numeric Rating Scale (NRS). Functional limitations include inability
to sit for prolonged periods at her clerical job, sleep disruption from
nocturnal pain, and difficulty climbing stairs.

PAST MEDICAL HISTORY
- Lumbar disc herniation (L4-L5, see imaging)
- Hyperlipidemia, controlled on atorvastatin
- Essential hypertension, controlled on amlodipine

CURRENT MEDICATIONS
- naproxen 500 mg orally twice daily (started 2025-10-15, ongoing) — NSAID
- gabapentin 300 mg orally three times daily (started 2025-11-01, ongoing) —
  anticonvulsant for neuropathic pain
- methocarbamol 750 mg orally three times daily (started 2025-12-01,
  ongoing) — muscle relaxant
- atorvastatin 20 mg orally daily
- amlodipine 5 mg orally daily

ALLERGIES
No known drug allergies. No contrast allergy. No corticosteroid allergy.
No bleeding disorder. Not on anticoagulation.

CONSERVATIVE THERAPY HISTORY
The patient completed a structured course of physical therapy at our PT
clinic. Documentation reflects:
- Start date: 2025-12-15
- End date: 2026-02-12
- Total sessions: 14
- Frequency: 3 sessions per week (1-hour sessions)
- Modalities: McKenzie extension exercises, lumbar stabilization, manual
  therapy, therapeutic ultrasound
- Outcome at completion: patient unable to achieve 50% pain reduction;
  pain returned to 8 out of 10 NRS within one week of completing PT
- Therapist note (Dr. P. Henderson, DPT): "Patient demonstrated good
  adherence and effort. Despite progression of program, persistent right
  L5 radicular symptoms refractory to conservative measures."

Activity modification: patient has been off her usual exercise regimen since
August 2025 and has restricted lifting to <10 lbs since October 2025.

PHYSICAL EXAM
Vitals: BP 128/82, HR 76, weight 64 kg, BMI 25.5
Spine: tenderness over right paraspinal muscles L4-S1.
Range of motion: lumbar flexion limited to 40° (normal 60°) reproducing
right leg pain. Extension limited to 15°.
Strength: right EHL 4/5 (mild weakness), left 5/5. Other myotomes intact.
Sensation: decreased sensation to light touch over right L5 dermatome.
Reflexes: 2+ symmetric patellar and Achilles. No clonus.
Straight leg raise: positive on right at 35° reproducing leg pain;
negative on left.

LABS AND IMAGING
- MRI Lumbar Spine without contrast, performed 2025-11-30:
    Impression: Right paracentral disc herniation at L4-L5 with mass
    effect on the descending right L5 nerve root. Mild facet arthropathy
    L5-S1 without significant central canal stenosis. No evidence of
    cord pathology or other abnormality.
- X-ray Lumbar Spine, AP and lateral, 2025-09-10: Mild degenerative
  changes; alignment maintained.
- Recent CBC and BMP within normal limits.
- Pregnancy test: not applicable (postmenopausal x 6 years).

ASSESSMENT AND PLAN
58yo postmenopausal female with imaging-confirmed right L4-L5 disc
herniation and L5 radiculopathy, refractory to a completed 8-week course
of physical therapy and ongoing pharmacotherapy with NSAIDs, gabapentin,
and muscle relaxant.

PLAN: Schedule for L4-L5 right transforaminal versus interlaminar
epidural steroid injection. Discussed risks (bleeding, infection,
dural puncture, paralysis, no improvement) and benefits. Patient
verbalized understanding and consent.

CPT 62323 — Lumbar interlaminar epidural injection with imaging guidance,
single level.
Primary diagnosis: M54.16 (Radiculopathy, lumbar region).

Anti-coagulants: No
Contrast allergy: No
Steroid allergy: No
Pregnancy: No (postmenopausal)
""",
)
