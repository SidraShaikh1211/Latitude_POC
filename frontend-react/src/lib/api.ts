import type {
  CaseDetail,
  CaseSummary,
  DoctorSubmitResponse,
  PolicyDetail,
  PolicySummary,
  Submission,
} from "@/types/api";

export const API_BASE: string =
  import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}${body ? ` — ${body}` : ""}`);
  }
  return res.json() as Promise<T>;
}

export async function getHealth(): Promise<{ status: string }> {
  const res = await fetch(`${API_BASE}/health`);
  return json(res);
}

export async function listPolicies(): Promise<PolicySummary[]> {
  return json(await fetch(`${API_BASE}/v1/policies`));
}

export async function getPolicy(policyId: string): Promise<PolicyDetail> {
  return json(await fetch(`${API_BASE}/v1/policies/${policyId}`));
}

export async function listCases(): Promise<CaseSummary[]> {
  return json(await fetch(`${API_BASE}/v1/cases`));
}

export async function getCase(caseId: string): Promise<CaseDetail> {
  return json(await fetch(`${API_BASE}/v1/cases/${caseId}`));
}

export async function submitDoctorPdf(
  pdf: File,
): Promise<DoctorSubmitResponse> {
  const fd = new FormData();
  fd.append("pdf", pdf, pdf.name);
  const res = await fetch(`${API_BASE}/v1/doctor/submit`, {
    method: "POST",
    body: fd,
  });
  return json(res);
}

export async function getSubmission(submissionId: string): Promise<Submission> {
  return json(await fetch(`${API_BASE}/v1/doctor/submissions/${submissionId}`));
}

export async function listSubmissions(): Promise<Submission[]> {
  return json(await fetch(`${API_BASE}/v1/doctor/submissions`));
}
