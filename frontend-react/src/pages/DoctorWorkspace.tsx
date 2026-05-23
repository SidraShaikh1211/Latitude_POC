import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
  ChevronRight,
  FileUp,
  Loader2,
  Send,
  Upload,
} from "lucide-react";

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { ExtractedMetadataPanel } from "@/components/ExtractedMetadataPanel";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { getPolicy, listSubmissions, submitDoctorPdf } from "@/lib/api";
import { useCaseStream, useSubmissionStream } from "@/lib/sse";
import type { CriterionNode, PolicyDetail } from "@/types/api";
import type {
  CaseDetail,
  ProcessingStage,
  Submission,
  SubmissionState,
} from "@/types/api";

// ---------------------------------------------------------------------------
// Stage maps — doctor side (Submission) is distinct from payer side (Case)
// ---------------------------------------------------------------------------

// Doctor side: 0% → ~40% covers everything the doctor controls.
const DOCTOR_STAGE: Record<SubmissionState, { label: string; pct: number }> = {
  extracting_metadata: { label: "📋 Reading your PDF", pct: 8 },
  bundle_ready: { label: "📦 FHIR Bundle assembled", pct: 25 },
  sending: { label: "📡 Sending Bundle to payer (PAS $submit)", pct: 35 },
  sent: { label: "✉️ Payer received the Bundle", pct: 40 },
  failed: { label: "❌ Doctor-side failure", pct: 100 },
};

// Payer side: 45% → 100% — only meaningful once we know the payer_case_id.
const PAYER_STAGE: Record<ProcessingStage, { label: string; pct: number }> = {
  extracting_metadata: { label: "(unused on payer)", pct: 40 },
  received: { label: "📨 Payer received request", pct: 45 },
  parsing: { label: "📨 Parsing Bundle", pct: 50 },
  intake: { label: "🔍 Intake — extracting FHIR facts", pct: 60 },
  selecting: { label: "🔍 Selecting policy", pct: 68 },
  adjudicating: { label: "🔍 Adjudicating criteria (per-leaf LLM)", pct: 82 },
  reviewing: { label: "🔍 Reviewer drafting narrative", pct: 92 },
  building_response: { label: "🔍 Building ClaimResponse Bundle", pct: 97 },
  complete: { label: "✅ Determination complete", pct: 100 },
  failed: { label: "❌ Payer-side failure", pct: 100 },
};

function secondsAgo(iso: string): number {
  return Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function DoctorWorkspace() {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingFile, setPendingFile] = useState<File | null>(null);
  // Submission ids the user posted in this browser session — we surface
  // these at the top of the list, persisted across reloads via the backend.
  const [sessionSubmissions, setSessionSubmissions] = useState<string[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  // All submissions from the backend (so a refresh doesn't lose them).
  const all = useQuery({
    queryKey: ["submissions"],
    queryFn: listSubmissions,
    refetchInterval: 5_000,
  });

  async function handleSubmit() {
    if (!pendingFile) return;
    setError(null);
    setSubmitting(true);
    try {
      const resp = await submitDoctorPdf(pendingFile);
      setSessionSubmissions((s) => [resp.submission_id, ...s]);
      // Trigger an immediate refetch of the list so the new row appears.
      void all.refetch();
      setPendingFile(null);
      if (fileRef.current) fileRef.current.value = "";
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  const submissions = all.data ?? [];

  return (
    <div className="space-y-8">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          🩺 Doctor Workspace
        </h1>
        <p className="text-muted-foreground max-w-3xl">
          Upload a patient's clinical PDF. The system extracts patient
          demographics, coverage info, ICD-10 diagnoses, and infers the
          requested CPT code from clinical context. Then a Da Vinci PAS Claim
          Bundle is assembled and{" "}
          <strong>POSTed to the payer's <code>/fhir/Claim/$submit</code></strong>{" "}
          endpoint — the doctor → payer handoff is a real HTTP call.
        </p>
      </header>

      {/* Upload */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <FileUp className="h-4 w-4" /> Upload a clinical PDF
          </CardTitle>
          <CardDescription>
            Drop a patient's clinical document (H&amp;P, fax bundle, progress
            notes). No form fields needed.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <input
            ref={fileRef}
            type="file"
            accept="application/pdf"
            onChange={(e) => setPendingFile(e.target.files?.[0] ?? null)}
            className="block w-full text-sm file:mr-4 file:rounded-md file:border-0 file:bg-primary file:text-primary-foreground file:px-3 file:py-1.5 file:font-medium hover:file:bg-primary/90"
          />
          <div className="flex items-center gap-3">
            <Button
              onClick={handleSubmit}
              disabled={!pendingFile || submitting}
            >
              {submitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Uploading…
                </>
              ) : (
                <>
                  <Upload className="h-4 w-4" />
                  Submit
                </>
              )}
            </Button>
            {pendingFile && (
              <span className="text-sm text-muted-foreground truncate">
                {pendingFile.name} · {(pendingFile.size / 1024).toFixed(0)} KB
              </span>
            )}
          </div>
          {error && (
            <Alert variant="destructive">
              <AlertCircle className="h-4 w-4" />
              <AlertTitle>Submission failed</AlertTitle>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      {/* Submissions */}
      <section className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 className="text-xl font-semibold">📊 My submissions (live)</h2>
          <p className="text-xs text-muted-foreground">
            Doctor view — payer cases only appear after the Bundle is sent.
          </p>
        </div>
        {submissions.length === 0 ? (
          <Alert variant="info">
            <AlertDescription>
              Upload a PDF above to see live status here.
            </AlertDescription>
          </Alert>
        ) : (
          <SubmissionsList
            submissions={submissions}
            highlight={sessionSubmissions}
          />
        )}
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Submissions list — per-submission SSE subscriptions (no polling)
// ---------------------------------------------------------------------------

function SubmissionsList({
  submissions,
  highlight,
}: {
  submissions: Submission[];
  highlight: string[];
}) {
  // Order: this-session submissions first, then the rest by updated_at.
  const ordered = [
    ...submissions.filter((s) => highlight.includes(s.submission_id)),
    ...submissions.filter((s) => !highlight.includes(s.submission_id)),
  ];

  return (
    <div className="space-y-4">
      {ordered.map((s) => (
        <LiveSubmissionCard key={s.submission_id} initial={s} />
      ))}
    </div>
  );
}

// Subscribes to the submission's SSE stream; once the submission has handed
// off to a payer case, also subscribes to the case stream. Both EventSources
// auto-close when their topic reaches a terminal state.
function LiveSubmissionCard({ initial }: { initial: Submission }) {
  const sub = useSubmissionStream(initial.submission_id, initial) ?? initial;
  const caseData = useCaseStream(sub.payer_case_id ?? null);
  return <SubmissionCard submission={sub} caseData={caseData} />;
}

// ---------------------------------------------------------------------------
// One submission card
// ---------------------------------------------------------------------------

function SubmissionCard({
  submission,
  caseData,
}: {
  submission: Submission;
  caseData: CaseDetail | undefined;
}) {
  // 1Hz tick so "Submitted Ns ago" updates without refetch.
  const [, force] = useState(0);
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const submittedAt = submission.created_at ?? new Date().toISOString();
  const elapsed = secondsAgo(submittedAt);

  // The card has two halves now: doctor-side (always shown) + payer-side
  // (only shown once handoff has happened).
  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex items-center gap-3">
            <CardTitle className="text-base">
              Submission{" "}
              <code className="text-primary">{submission.submission_id}</code>
            </CardTitle>
            <ActiveSpinner submission={submission} caseData={caseData} />
          </div>
          <p className="text-xs text-muted-foreground">
            {submission.patient_display && (
              <>Patient {submission.patient_display} · </>
            )}
            {submission.cpt_code && (
              <>
                CPT <code>{submission.cpt_code}</code> ·{" "}
              </>
            )}
            Submitted {elapsed}s ago
          </p>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        <DoctorPhase submission={submission} />
        {submission.state === "sent" && (
          <PayerPhase submission={submission} caseData={caseData} />
        )}
        {submission.state === "failed" && (
          <Alert variant="destructive">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Doctor-side failure</AlertTitle>
            <AlertDescription>
              {submission.error_message ?? "Unknown error"}
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

function ActiveSpinner({
  submission,
  caseData,
}: {
  submission: Submission;
  caseData: CaseDetail | undefined;
}) {
  const doctorActive =
    submission.state !== "sent" && submission.state !== "failed";
  const payerActive =
    submission.state === "sent" &&
    caseData &&
    caseData.processing_stage !== "complete" &&
    caseData.processing_stage !== "failed";
  if (doctorActive || payerActive) {
    return <Loader2 className="h-4 w-4 animate-spin text-primary" />;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Doctor-side phase
// ---------------------------------------------------------------------------

function DoctorPhase({ submission }: { submission: Submission }) {
  const info = DOCTOR_STAGE[submission.state];
  const isDoctorActive =
    submission.state !== "sent" && submission.state !== "failed";

  return (
    <div className="rounded-md border border-sky-200 bg-sky-50/30 p-4 space-y-3">
      <header className="flex items-center justify-between">
        <Badge variant="info">🩺 Doctor side</Badge>
        {!isDoctorActive && submission.state === "sent" && (
          <Badge variant="success">✓ Handed off</Badge>
        )}
      </header>

      <div className="flex items-center justify-between">
        <p className="font-medium text-sm">{info.label}</p>
        <span className="text-xs text-muted-foreground">{info.pct}%</span>
      </div>
      <Progress value={info.pct} />

      <p className="text-xs text-muted-foreground">
        {submission.state === "extracting_metadata" &&
          "Claude is reading the PDF and inferring CPT codes, patient info, and diagnoses."}
        {submission.state === "bundle_ready" &&
          "Bundle assembled. Preparing to POST to the payer's $submit endpoint."}
        {submission.state === "sending" &&
          "HTTP POST in flight: the Bundle is on the wire."}
        {submission.state === "sent" &&
          `Payer accepted the Bundle. Case id: ${submission.payer_case_id}`}
      </p>

      <ExtractedMetadataPanel
        metadata={submission.extracted_metadata ?? undefined}
        notes={submission.extraction_notes ?? undefined}
      />

      {submission.bundle_preview && (
        <Accordion type="single" collapsible>
          <AccordionItem value="bundle">
            <AccordionTrigger>
              <span className="flex items-center gap-2 text-sm">
                <Send className="h-3.5 w-3.5" />
                📦 Outbound FHIR Bundle ({submission.bundle_entry_count} entries
                {submission.bundle_size_bytes &&
                  `, ${submission.bundle_size_bytes.toLocaleString()} bytes`}
                )
              </span>
            </AccordionTrigger>
            <AccordionContent>
              <p className="text-xs text-muted-foreground mb-2">
                The Da Vinci PAS Claim Bundle assembled from the extracted
                metadata. POSTed to <code>/fhir/Claim/$submit</code>.
              </p>
              <pre className="quote-block max-h-80 overflow-auto">
                {JSON.stringify(submission.bundle_preview, null, 2)}
              </pre>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Payer-side phase (only shown once submission.state === "sent")
// ---------------------------------------------------------------------------

function PayerPhase({
  submission,
  caseData,
}: {
  submission: Submission;
  caseData: CaseDetail | undefined;
}) {
  if (!submission.payer_case_id) return null;

  const stage = (caseData?.processing_stage ?? "received") as ProcessingStage;
  const info = PAYER_STAGE[stage];
  const terminal = stage === "complete" || stage === "failed";

  return (
    <div className="rounded-md border border-violet-200 bg-violet-50/30 p-4 space-y-3">
      <header className="flex items-center justify-between flex-wrap gap-2">
        <Badge variant="violet">🏥 Payer side</Badge>
        <p className="text-xs text-muted-foreground">
          Payer case{" "}
          <code className="text-violet-900">{submission.payer_case_id}</code>
        </p>
      </header>

      {!caseData ? (
        <p className="text-sm text-muted-foreground">
          Waiting for payer to acknowledge…
        </p>
      ) : terminal ? (
        <PayerComplete caseDetail={caseData} payerCaseId={submission.payer_case_id} />
      ) : (
        <>
          <div className="flex items-center justify-between">
            <p className="font-medium text-sm">{info.label}</p>
            <span className="text-xs text-muted-foreground">{info.pct}%</span>
          </div>
          <Progress value={info.pct} />
          <p className="text-xs text-muted-foreground">
            The payer is analyzing your request — extracting clinical facts,
            matching against policy criteria, drafting a narrative review.
          </p>
        </>
      )}
    </div>
  );
}

function PayerComplete({
  caseDetail,
  payerCaseId,
}: {
  caseDetail: CaseDetail;
  payerCaseId: string;
}) {
  if (caseDetail.processing_stage === "failed") {
    return (
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertTitle>Payer-side failure</AlertTitle>
        <AlertDescription>
          {caseDetail.error_message ?? "Unknown error"}
        </AlertDescription>
      </Alert>
    );
  }
  return (
    <DoctorOutcome caseDetail={caseDetail} payerCaseId={payerCaseId} />
  );
}

// ---------------------------------------------------------------------------
// Doctor-facing outcome view
// ---------------------------------------------------------------------------
//
// Distinct from the payer detail page: this surface answers the doctor's two
// questions — "what was the decision?" and "what do I need to send next?" —
// and anchors each answer with the verbatim policy text and the matching
// patient-document quote so they can see exactly what the payer saw.
function DoctorOutcome({
  caseDetail,
  payerCaseId,
}: {
  caseDetail: CaseDetail;
  payerCaseId: string;
}) {
  const det = caseDetail.determination;
  const policyId = caseDetail.selected_policy_id ?? undefined;

  // Fetch the selected policy so we can resolve each missing-info's
  // criterion_id back to its verbatim policy quote.
  const policyQuery = useQuery({
    queryKey: ["policy-detail", policyId],
    queryFn: () => getPolicy(policyId!),
    enabled: !!policyId,
    staleTime: 5 * 60 * 1000,
  });

  if (!det) {
    return (
      <p className="text-sm text-muted-foreground">
        Awaiting the payer's determination…
      </p>
    );
  }

  // Build the gap list: one entry per missing-info item, hydrated with the
  // policy citation (from the policy tree) and the patient evidence (from
  // the adjudicator's leaf verdict).
  const policyIndex = policyQuery.data
    ? indexPolicyByCriterion(policyQuery.data)
    : new Map<string, { node: CriterionNode; path: string[] }>();
  const leafVerdicts = caseDetail.criteria_evaluation?.leaf_verdicts ?? {};
  const gaps = (det.missing_info ?? []).map((m) => {
    const policyHit = policyIndex.get(m.criterion_id);
    const verdict = leafVerdicts[m.criterion_id];
    return {
      ...m,
      policy_citation: policyHit?.node.policy_citation ?? null,
      criterion_label: policyHit?.node.description ?? m.criterion_id,
      patient_evidence:
        (verdict?.patient_evidence ?? []).filter((e) => e.quote)[0] ?? null,
      gap_reasoning: verdict?.reasoning ?? null,
    };
  });

  const isPend = caseDetail.outcome === "pend";
  const isDeny = caseDetail.outcome === "deny";
  const isApprove = caseDetail.outcome === "approve";

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <OutcomeBadge outcome={caseDetail.outcome} />
        {policyId && (
          <p className="text-xs text-muted-foreground">
            Reviewed against{" "}
            <code className="text-violet-900">{policyId}</code>
            {caseDetail.branch && (
              <>
                {" "}· branch <code>{caseDetail.branch}</code>
              </>
            )}
          </p>
        )}
      </div>

      {/* One-line summary above the detail */}
      <p className="text-sm leading-relaxed">
        {isApprove && (
          <>
            <strong>Approved.</strong> The payer found every required criterion
            documented in your submission.
          </>
        )}
        {isDeny && (
          <>
            <strong>Denied.</strong>{" "}
            {det.rationale ??
              "The payer found an exclusion or a critical missing criterion."}
          </>
        )}
        {isPend && (
          <>
            <strong>Pended.</strong> The payer could not complete the
            determination with the documentation you provided. Please respond
            to the items below.
          </>
        )}
      </p>

      {/* Per-gap detail — only the failing items, each with dual citations */}
      {gaps.length > 0 && (
        <div className="space-y-3">
          <h4 className="font-semibold text-sm">
            📨 What you need to send ({gaps.length})
          </h4>
          {gaps.map((g) => (
            <div
              key={g.id}
              className="rounded-md border border-amber-200 bg-amber-50/60 p-3 space-y-2"
            >
              <p className="text-sm">
                <strong>{g.request}</strong>
              </p>
              {g.gap_reasoning && (
                <p className="text-xs text-muted-foreground leading-relaxed">
                  {g.gap_reasoning}
                </p>
              )}
              <div className="grid gap-2 md:grid-cols-2 text-xs">
                {g.policy_citation && (
                  <div className="rounded bg-white border p-2">
                    <p className="font-semibold mb-1 text-violet-900">
                      Policy says (p.{g.policy_citation.page})
                    </p>
                    <p className="text-muted-foreground italic">
                      "{g.policy_citation.quote}"
                    </p>
                  </div>
                )}
                {g.patient_evidence?.quote && (
                  <div className="rounded bg-white border p-2">
                    <p className="font-semibold mb-1 text-sky-900">
                      Your record (p.{g.patient_evidence.page ?? "?"})
                    </p>
                    <p className="text-muted-foreground italic">
                      "{g.patient_evidence.quote}"
                    </p>
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Triggered exclusions (deny path) — show with policy citation */}
      {isDeny &&
        det.triggered_exclusions &&
        det.triggered_exclusions.length > 0 && (
          <div className="space-y-2">
            <h4 className="font-semibold text-sm">⛔ Triggered exclusions</h4>
            {det.triggered_exclusions.map((exId) => {
              const ex = policyQuery.data?.exclusions.find((e) => e.id === exId);
              return (
                <div
                  key={exId}
                  className="rounded-md border border-red-200 bg-red-50/60 p-3 text-xs space-y-1"
                >
                  <p className="font-semibold text-red-900">
                    {exId} {ex?.description && `— ${ex.description}`}
                  </p>
                  {ex?.policy_citation && (
                    <p className="text-muted-foreground italic">
                      Policy (p.{ex.policy_citation.page}): "
                      {ex.policy_citation.quote}"
                    </p>
                  )}
                </div>
              );
            })}
          </div>
        )}

      <Button asChild variant="outline" size="sm">
        <Link to={`/payer/${payerCaseId}`}>
          View full payer detail <ChevronRight className="h-4 w-4" />
        </Link>
      </Button>
    </div>
  );
}

// Flatten the policy criteria tree into a criterion_id → {node, path} map so
// we can resolve a missing-info's criterion_id back to its policy citation.
function indexPolicyByCriterion(
  policy: PolicyDetail,
): Map<string, { node: CriterionNode; path: string[] }> {
  const out = new Map<string, { node: CriterionNode; path: string[] }>();
  const walk = (node: CriterionNode, path: string[]) => {
    out.set(node.id, { node, path });
    for (const c of node.children ?? []) walk(c, [...path, node.id]);
  };
  walk(policy.criteria, []);
  return out;
}
