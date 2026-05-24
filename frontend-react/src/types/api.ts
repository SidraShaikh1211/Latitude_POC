// Shapes returned by the FastAPI backend.
// Kept narrow on purpose: only fields the UI reads are typed.

export type Verdict = "met" | "not_met" | "unclear" | "not_documented";
export type Outcome = "approve" | "deny" | "pend" | "needs_human_review";
export type CaseStatus =
  | "approved"
  | "denied"
  | "pended"
  | "needs_review"
  | "processing"
  | "failed"
  | "unknown";

export type ProcessingStage =
  | "extracting_metadata"
  | "received"
  | "parsing"
  | "intake"
  | "selecting"
  | "adjudicating"
  | "reviewing"
  | "building_response"
  | "complete"
  | "failed";

export interface PolicySummary {
  policy_id: string;
  name: string;
  payer_id: string;
  version: string;
  effective_from: string;
  effective_until: string | null;
  cpt_codes: string[];
  lines_of_business: string[];
  states: string[];
  leaf_count: number;
  exclusion_count: number;
}

export interface PolicyCitation {
  page: number;
  section: string | null;
  quote: string;
}

export interface CriterionNode {
  id: string;
  type: "leaf" | "internal";
  operator?: "ALL" | "ONE_OF" | "NOT" | "AT_LEAST_K";
  description: string;
  policy_citation: PolicyCitation | null;
  evaluation?: unknown;
  verdict_rubric?: unknown;
  children?: CriterionNode[];
}

export interface PolicyDetail extends PolicySummary {
  applies_to: {
    cpt_codes: string[];
    icd10_patterns: string[];
    lines_of_business: string[];
    states: string[];
    age_min: number | null;
    settings_of_care: string[];
  };
  criteria: CriterionNode;
  exclusions: Array<{
    id: string;
    description: string;
    policy_citation: PolicyCitation;
  }>;
  metadata: Record<string, unknown>;
}

export interface PatientEvidence {
  fact_type?: string;
  value_summary?: string;
  page?: number;
  quote?: string;
}

export interface LeafVerdict {
  verdict: Verdict;
  confidence?: number;
  reasoning?: string;
  patient_evidence?: PatientEvidence[];
  missing_info?: string[];
}

export interface MissingInfoItem {
  id: string;
  criterion_id: string;
  request: string;
}

export interface DeterminationBlock {
  outcome: Outcome;
  rationale?: string;
  triggered_exclusions?: string[];
  escalation_reasons?: string[];
  narrative?: string;
  missing_info?: MissingInfoItem[];
}

export interface Citation {
  page: number;
  section: string | null;
  quote: string;
  extraction_confidence?: number;
}

export interface ExtractedFactItem {
  display?: string;
  code_display?: string;
  medication_name?: string;
  substance?: string;
  findings?: string;
  icd10_code?: string;
  value_string?: string;
  dose?: string;
  cpt_code?: string;
  reaction?: string;
  modality?: string;
  citations?: Citation[];
}

export interface ExtractedFacts {
  patient?: {
    given?: string;
    family?: string;
    birth_date?: string;
    gender?: string;
    mrn?: string;
    citations?: Citation[];
  };
  conditions?: ExtractedFactItem[];
  observations?: ExtractedFactItem[];
  medications?: ExtractedFactItem[];
  procedures?: ExtractedFactItem[];
  allergies?: ExtractedFactItem[];
  diagnostic_reports?: ExtractedFactItem[];
}

export interface CaseSummary {
  case_id: string;
  status: CaseStatus;
  outcome: Outcome | null;
  selected_policy_id: string | null;
  branch: string | null;
  patient_display: string | null;
  cpt_code: string | null;
  payer_id: string | null;
}

export interface CaseDetail extends CaseSummary {
  processing_stage: ProcessingStage | null;
  error_message: string | null;
  extracted_facts: ExtractedFacts | null;
  policy_selection: {
    status: string;
    selected_policy_id: string | null;
    branch: string | null;
    selection_reason: string;
    eliminated: Array<[string, string]>;
  } | null;
  criteria_evaluation: {
    leaf_verdicts: Record<string, LeafVerdict>;
    exclusion_verdicts: Record<string, LeafVerdict>;
  } | null;
  determination: DeterminationBlock | null;
  pas_response_bundle: Record<string, unknown> | null;
  created_at: string | null;
  updated_at: string | null;
}

// Doctor-side submission states (separate from the payer's Case).
//
// Lifecycle:
//   extracting_metadata → bundle_ready → sending → sent
//       → awaiting_payer_response → payer_responded
//                                 ↘ payer_failed
//                                 ↘ failed (doctor-side)
export type SubmissionState =
  | "extracting_metadata"
  | "bundle_ready"
  | "sending"
  | "sent"
  | "awaiting_payer_response"
  | "payer_responded"
  | "payer_failed"
  | "failed";

export interface Submission {
  submission_id: string;
  state: SubmissionState;
  pdf_filename: string | null;
  error_message: string | null;
  extracted_metadata: ExtractedMetadata | null;
  extraction_notes: string | null;
  metadata_cost_usd: number | null;
  bundle_entry_count: number | null;
  bundle_size_bytes: number | null;
  bundle_preview: Record<string, unknown> | null;
  patient_display: string | null;
  cpt_code: string | null;
  payer_case_id: string | null;
  // Populated by the payer's ClaimResponse callback (POST
  // /v1/doctor/inbound/claim-response). The doctor UI reads these directly
  // off the Submission row — no cross-coupling to the payer's Case.
  outcome: Outcome | null;
  determination_narrative: string | null;
  missing_info: Array<{ id: string; criterion_id: string; request: string }> | null;
  claim_response_bundle: Record<string, unknown> | null;
  payer_responded_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

// Doctor submit response (POST /v1/doctor/submit)
export interface ExtractedMetadata {
  patient?: {
    patient_given?: string;
    patient_family?: string;
    patient_dob?: string;
    patient_gender?: string;
    patient_state?: string;
  };
  coverage?: {
    payer_id?: string;
    payer_display?: string;
    member_id?: string;
    line_of_business?: string;
    plan_name?: string;
  };
  service_request?: {
    cpt_code?: string;
    cpt_display?: string;
    service_date?: string;
    body_site_display?: string;
    icd10_codes?: Array<{ code: string; kind: string; display: string }>;
  };
}

export interface DoctorSubmitResponse {
  submission_id: string;
  state: SubmissionState;
  pdf_filename: string;
}

// Performance / metrics — one entry per end-to-end run (doctor + payer joined).

export interface MetricsStage {
  name: string;
  side?: "doctor" | "payer";
  duration_seconds: number;
  tokens_in: number;
  tokens_out: number;
  cache_read: number;
  cache_creation: number;
  cost_usd: number;
  llm_calls: number;
}

export interface AdjudicationLeafMetric {
  criterion_id: string;
  kind: "leaf" | "exclusion";
  verdict: Verdict | null;
  iterations: number;
  tokens_in: number;
  tokens_out: number;
  cache_read: number;
  cache_creation: number;
  cost_usd: number;
}

export interface MetricsTotals {
  tokens_in: number;
  tokens_out: number;
  cache_read: number;
  cache_creation: number;
  cost_usd: number;
  llm_calls: number;
  cache_hit_rate: number;
}

export interface PerformanceRun {
  submission_id: string;
  payer_case_id: string | null;
  submission_state: SubmissionState;
  case_status: CaseStatus | null;
  outcome: Outcome | null;
  patient_display: string | null;
  cpt_code: string | null;
  selected_policy_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  totals: MetricsTotals;
  duration_seconds: number;
  doctor_duration_seconds: number;
  payer_duration_seconds: number;
  stages: MetricsStage[];
  adjudication_leaves: AdjudicationLeafMetric[];
}
