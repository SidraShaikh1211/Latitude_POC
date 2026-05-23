"""L3-ADN-C1 — Clean approve case for Oregon adenomyosis hysterectomy.

Diane Foster, 46F, Oregon Health Authority Medicaid. Imaging-confirmed
adenomyosis, completed 6+ months of hormonal + NSAID trial, all checklist
criteria met. Expected: approve. NOT the Welsh case.
"""

from tests.evals.level3_e2e.synthetic_bundle import L3SyntheticCase


CASE = L3SyntheticCase(
    id="L3-ADN-C1",
    domain="adenomyosis",

    family="Foster",
    given="Diane",
    birth_date="1979-11-08",
    gender="female",
    state="OR",
    mrn="MRN-ADN-C1",

    payer_id="oregon-hca",
    payer_display="Oregon Health Authority — Health Plan",
    lob="Medicaid",
    member_id="OHA-C1-042",

    cpt_code="58570",
    service_display="Laparoscopy, surgical, with total hysterectomy, for uterus 250 g or less",
    request_category="surgical",
    icd10_codes=["N80.03", "N94.6"],
    service_date="2026-05-15",

    expected_outcome="approve",
    expected_info_keywords=[],
    expected_deny_keywords=[],

    notes="Clean adenomyosis approve: confirmed adenomyosis on MRI (junctional zone 14 mm), >6 months of dysmenorrhea with QoL impact, 8-month OCP trial completed, ibuprofen for 9 months, NILM pap, negative hCG, premenopausal not sterilized. Different patient than Welsh.",

    narrative="""\
CHIEF COMPLAINT
46-year-old woman with imaging-confirmed adenomyosis seeking total
laparoscopic hysterectomy for treatment-refractory pelvic pain.

HISTORY OF PRESENT ILLNESS
Diane Foster is a 46-year-old G2P2 premenopausal woman with a 14-month
history of progressively severe dysmenorrhea, chronic pelvic pain, and
abnormal uterine bleeding. Symptoms began in February 2025 with cyclical
crampy pelvic pain peaking with menses, and have escalated to include
constant pelvic ache, dyspareunia, and heavy menstrual bleeding (changing
pad/tampon every 1-2 hours for the first 3 days of cycle).

The patient describes significant negative impact on quality of life:
- Missing 3-5 work days per month due to pain and bleeding
- Discontinuation of regular exercise (formerly a runner)
- Significant impact on intimacy with partner (dyspareunia at 7/10 NRS)
- Sleep disruption from nocturnal pelvic pain
- Anxiety related to predicting menstrual flooding episodes

She has trialed and failed an extended course of medical management (see
conservative therapy section below). She is now requesting definitive
surgical treatment with total laparoscopic hysterectomy (TLH) plus
bilateral salpingectomy. She has completed her family and does not desire
future pregnancies.

PAST MEDICAL HISTORY
- Adenomyosis (N80.03)
- Dysmenorrhea (N94.6)
- Abnormal uterine bleeding (heavy menstrual bleeding pattern)
- Iron deficiency anemia secondary to menorrhagia, currently on iron
  supplementation
- Mild anxiety, well-controlled

PAST SURGICAL HISTORY
- Diagnostic laparoscopy 2023 — minimal adhesions, no endometriosis seen
- Cesarean section x 2 (2008, 2011)
- No prior tubal sterilization or salpingectomy

CURRENT MEDICATIONS
- ibuprofen 600 mg orally three times daily as needed for pelvic pain
  (taking essentially daily; started 2024-12-01) — NSAID
- ferrous sulfate 325 mg orally twice daily for iron deficiency
- sertraline 50 mg orally daily for mild anxiety
- daily prenatal multivitamin (continues from prior pregnancy planning)

Discontinued medications:
- norethindrone-ethinyl estradiol (Ortho-Novum 0.5/35) 0.5 mg/35 mcg
  orally once daily, started 2024-09-15, discontinued 2025-05-30 (8.5
  months of trial) — OCP, hormonal therapy. Discontinued due to
  inadequate symptom relief despite full adherent course.

ALLERGIES
No known drug allergies. No NSAID allergy. No latex allergy.

CONSERVATIVE THERAPY HISTORY
The patient completed a >6-month therapeutic trial of both required
medication classes:

1) Hormonal therapy: norethindrone-ethinyl estradiol (Ortho-Novum
   0.5/35) 0.5 mg/35 mcg orally daily from 2024-09-15 through
   2025-05-30 (8.5 months). Patient was 100% adherent (verified by
   pharmacy claims). At 6-month follow-up, dysmenorrhea remained at
   8 out of 10 NRS during menses despite the OCP trial. Decision made
   to discontinue OCP in May 2025 given lack of response.

2) Nonsteroidal anti-inflammatory drugs: ibuprofen 600 mg orally three
   times daily started 2024-12-01 and continuing (now 9 months total
   trial duration). Provides partial symptom relief but pain persists
   at unacceptable levels with significant ADL impact.

No documented contraindications to either hormonal therapy or NSAIDs
that would have precluded these trials.

PHYSICAL EXAM
Vitals: BP 118/76, HR 72, weight 68 kg, BMI 24.7
General: well-appearing in no acute distress.
Abdomen: soft, mildly tender suprapubic, no rebound, no masses palpable.
Pelvic: external genitalia normal. Speculum: cervix grossly normal. Bimanual:
uterus globular, mildly enlarged (~10-week size), tender to palpation.
Adnexa non-tender, no palpable masses.

LABS AND IMAGING

Pap smear (cervical cytology) — performed 2025-07-22:
   Result: Negative for intraepithelial lesion or malignancy (NILM).
   HPV co-test: negative.
   Conclusion: nonmalignant cervical cytology, within standard screening
   interval.

Serum β-hCG — performed 2026-05-01 (14 days pre-op):
   Result: negative (<5 mIU/mL).
   Note: patient is premenopausal and has not been previously sterilized.

MRI Pelvis with and without contrast — performed 2026-02-14:
   Findings: The uterus is mildly enlarged measuring 11.2 x 7.4 x 6.1 cm.
   Diffuse thickening of the junctional zone is noted, measuring 14 mm
   in maximum thickness (normal <8 mm; >12 mm consistent with
   adenomyosis). T2 hyperintense myometrial cysts present. Endometrium
   is normal in thickness. Ovaries appear normal bilaterally. No focal
   masses. No evidence of endometriosis. No hydrosalpinx or hydronephrosis.

   Impression: Imaging findings are consistent with diffuse adenomyosis.
   No findings suggestive of malignancy or endometriosis.

CBC: Hemoglobin 10.6 g/dL (mild anemia consistent with menorrhagia),
     improving on iron therapy; otherwise within normal limits.
CMP: within normal limits.
TSH: 1.8 mIU/L (within normal limits).

ASSESSMENT AND PLAN
46yo G2P2 premenopausal woman with imaging-confirmed diffuse adenomyosis
(MRI junctional zone 14 mm), 14 months of progressively severe dysmenorrhea
and pelvic pain with significant quality-of-life impact, failed 8.5-month
trial of OCP hormonal therapy AND 9-month ongoing NSAID trial, with no
contraindications to medical therapy. Cervical cytology nonmalignant. Pre-op
β-hCG negative.

PLAN: Schedule total laparoscopic hysterectomy with bilateral salpingectomy
(TLH/BS), CPT 58570. Anticipated date of service 2026-05-15. Pre-op
clearance completed. Patient has provided informed consent including
discussion of risks (bleeding, infection, injury to bowel/bladder/ureter,
conversion to open, need for transfusion, risks of anesthesia) and
alternatives (continued medical management, hysteroscopic options not
indicated for diffuse adenomyosis).

Primary indication: N80.03 (Adenomyosis), with N94.6 (dysmenorrhea) as
secondary.

The patient has completed the documentation requirements for the Oregon
Health Authority Prioritized List Guideline Note 39, Section B
(Hysterectomy for adenomyosis).
""",
)
