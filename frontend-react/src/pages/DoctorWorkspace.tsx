import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQueries, useQuery } from "@tanstack/react-query";
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
import {
  getCase,
  getSubmission,
  listSubmissions,
  submitDoctorPdf,
} from "@/lib/api";
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
// Submissions list — per-submission polling
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

  // Poll each *active* submission every 2s. Stops once state="sent" or "failed"
  // (the case-side polling takes over after that).
  const subQueries = useQueries({
    queries: ordered.map((s) => ({
      queryKey: ["submission", s.submission_id],
      queryFn: () => getSubmission(s.submission_id),
      initialData: s,
      refetchInterval: (q: { state: { data?: Submission } }) => {
        const st = q.state.data?.state;
        return st === "sent" || st === "failed" ? false : 2000;
      },
    })),
  });

  // For submissions that have handed off, poll the payer case too.
  const payerCaseIds = subQueries.map((q) => q.data?.payer_case_id ?? null);

  const caseQueries = useQueries({
    queries: ordered.map((_, i) => ({
      queryKey: ["case", payerCaseIds[i] ?? "none"],
      queryFn: () => getCase(payerCaseIds[i]!),
      enabled: !!payerCaseIds[i],
      refetchInterval: (q: { state: { data?: CaseDetail } }) => {
        const stage = q.state.data?.processing_stage;
        return stage === "complete" || stage === "failed" ? false : 2000;
      },
    })),
  });

  return (
    <div className="space-y-4">
      {ordered.map((s, i) => (
        <SubmissionCard
          key={s.submission_id}
          submission={subQueries[i].data ?? s}
          caseData={caseQueries[i].data}
        />
      ))}
    </div>
  );
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
  const det = caseDetail.determination;
  const missing = det?.missing_info ?? [];
  return (
    <div className="space-y-4">
      <OutcomeBadge outcome={caseDetail.outcome} />
      {det?.narrative && (
        <div>
          <h4 className="font-semibold mb-1 text-sm">Reviewer narrative</h4>
          <p className="text-sm leading-relaxed whitespace-pre-wrap">
            {det.narrative}
          </p>
        </div>
      )}
      {missing.length > 0 && (
        <div className="space-y-2">
          <h4 className="font-semibold text-sm">
            📨 Missing information requested ({missing.length})
          </h4>
          {missing.map((m) => (
            <div key={m.id} className="rounded-md border p-3 bg-amber-50">
              <p className="text-sm">
                <strong>{m.id}</strong> — {m.request}
              </p>
              <p className="text-xs text-muted-foreground mt-1">
                Criterion <code>{m.criterion_id}</code>
              </p>
            </div>
          ))}
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
