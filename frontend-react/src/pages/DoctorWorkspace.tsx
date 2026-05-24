import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertCircle,
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
import { listSubmissions, submitDoctorPdf } from "@/lib/api";
import { useSubmissionStream } from "@/lib/sse";
import type { Submission, SubmissionState } from "@/types/api";

// ---------------------------------------------------------------------------
// Doctor-side stage map. The doctor's responsibility ends at "sent" — after
// that the payer owns the case and the doctor follows up on the payer page.
// ---------------------------------------------------------------------------

const DOCTOR_STAGE: Record<SubmissionState, { label: string; pct: number }> = {
  extracting_metadata: { label: "📋 Reading your PDF", pct: 15 },
  bundle_ready: { label: "📦 FHIR Bundle assembled", pct: 55 },
  sending: { label: "📡 Sending Bundle to payer (PAS $submit)", pct: 80 },
  sent: { label: "✉️ Payer received the Bundle", pct: 100 },
  failed: { label: "❌ Doctor-side failure", pct: 100 },
};

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
            Doctor view — upload, extract, and hand off to the payer.
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

// Subscribes to the submission's SSE stream until it reaches `sent` or
// `failed`. We deliberately do NOT subscribe to the payer case here — the
// payer's progress and outcome live on the payer page.
function LiveSubmissionCard({ initial }: { initial: Submission }) {
  const sub = useSubmissionStream(initial.submission_id, initial) ?? initial;
  return <SubmissionCard submission={sub} />;
}

// ---------------------------------------------------------------------------
// One submission card — strictly doctor-side
// ---------------------------------------------------------------------------

function SubmissionCard({ submission }: { submission: Submission }) {
  const doctorActive =
    submission.state !== "sent" && submission.state !== "failed";

  const metaParts: string[] = [];
  if (submission.patient_display) {
    metaParts.push(`Patient ${submission.patient_display}`);
  }
  if (submission.cpt_code) {
    metaParts.push(`CPT ${submission.cpt_code}`);
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex items-center gap-3">
            <CardTitle className="text-base">
              Submission{" "}
              <code className="text-primary">{submission.submission_id}</code>
            </CardTitle>
            {doctorActive && (
              <Loader2 className="h-4 w-4 animate-spin text-primary" />
            )}
          </div>
          {metaParts.length > 0 && (
            <p className="text-xs text-muted-foreground">
              {metaParts.join(" · ")}
            </p>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        <DoctorPhase submission={submission} />
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

// ---------------------------------------------------------------------------
// Doctor-side phase
// ---------------------------------------------------------------------------

function DoctorPhase({ submission }: { submission: Submission }) {
  const info = DOCTOR_STAGE[submission.state];

  return (
    <div className="rounded-md border border-sky-200 bg-sky-50/30 p-4 space-y-3">
      <header className="flex items-center justify-between">
        <Badge variant="info">🩺 Doctor side</Badge>
        {submission.state === "sent" && (
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
          "Bundle delivered. The payer now owns the case."}
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
