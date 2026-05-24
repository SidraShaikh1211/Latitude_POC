import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, FileText, Loader2 } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { CriteriaTree } from "@/components/CriteriaTree";
import { FhirPanel } from "@/components/FhirPanel";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { getCase, getPolicy } from "@/lib/api";
import { useCaseStream } from "@/lib/sse";
import type { CaseDetail, ProcessingStage } from "@/types/api";

// Payer-side in-flight stages — only payer activity is shown here.
const PAYER_STAGE: Record<ProcessingStage, { label: string; pct: number }> = {
  extracting_metadata: { label: "📋 Reading inbound Bundle", pct: 5 },
  received: { label: "📨 Bundle received from provider", pct: 10 },
  parsing: { label: "📨 Parsing Bundle", pct: 20 },
  intake: { label: "🔍 Intake — extracting FHIR facts", pct: 40 },
  selecting: { label: "🔍 Selecting policy", pct: 55 },
  adjudicating: { label: "🔍 Adjudicating criteria (per-leaf LLM)", pct: 75 },
  reviewing: { label: "🔍 Reviewer drafting narrative", pct: 88 },
  building_response: { label: "🔍 Building ClaimResponse Bundle", pct: 96 },
  complete: { label: "✅ Determination complete", pct: 100 },
  failed: { label: "❌ Payer-side failure", pct: 100 },
};

export function PayerCaseDetail() {
  const { caseId } = useParams<{ caseId: string }>();

  // Seed with a one-shot fetch so first paint is instant; the SSE stream
  // then drives every subsequent update as the payer works the case.
  const caseQ = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => getCase(caseId!),
    enabled: !!caseId,
  });
  const streamed = useCaseStream(caseId ?? null);
  const c: CaseDetail | undefined = streamed ?? caseQ.data;

  const policyId = c?.selected_policy_id ?? null;
  const policyQ = useQuery({
    queryKey: ["policy", policyId],
    queryFn: () => getPolicy(policyId!),
    enabled: !!policyId,
  });

  if (!caseId) {
    return (
      <Alert variant="destructive">
        <AlertDescription>No case id in URL.</AlertDescription>
      </Alert>
    );
  }
  if (!c && caseQ.isLoading) {
    return <p className="text-muted-foreground">Loading…</p>;
  }
  if (!c && caseQ.isError) {
    return (
      <Alert variant="destructive">
        <AlertDescription>
          {caseQ.error instanceof Error
            ? caseQ.error.message
            : "Could not load case"}
        </AlertDescription>
      </Alert>
    );
  }
  if (!c) return null;

  const stage = (c.processing_stage ?? "received") as ProcessingStage;
  const inFlight = stage !== "complete" && stage !== "failed";

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link to="/payer">
            <ArrowLeft className="h-4 w-4" /> Inbox
          </Link>
        </Button>
      </div>

      <header className="space-y-2">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          Case <code className="text-primary">{c.case_id}</code>
          {inFlight && (
            <Loader2 className="h-5 w-5 animate-spin text-primary" />
          )}
        </h1>
        <p className="text-sm text-muted-foreground flex flex-wrap gap-x-4 gap-y-1">
          <span>
            <strong>Patient:</strong> {c.patient_display ?? "—"}
          </span>
          <span>
            <strong>CPT:</strong>{" "}
            {c.cpt_code ? <code>{c.cpt_code}</code> : "—"}
          </span>
          <span>
            <strong>Payer:</strong> {c.payer_id ?? "—"}
          </span>
          <span>
            <strong>Policy:</strong>{" "}
            {c.selected_policy_id ? (
              <code>{c.selected_policy_id}</code>
            ) : (
              "—"
            )}
          </span>
          <span>
            <strong>Branch:</strong> {c.branch ?? "—"}
          </span>
        </p>
        {c.policy_selection && (
          <p className="text-xs text-muted-foreground">
            Selection: <Badge variant="outline">{c.policy_selection.status}</Badge>{" "}
            — {c.policy_selection.selection_reason}
          </p>
        )}
      </header>

      <PayerProgressBanner stage={stage} errorMessage={c.error_message} />

      <div className="grid lg:grid-cols-12 gap-6">
        <Card className="lg:col-span-5">
          <CardHeader>
            <CardTitle className="text-base">
              🗂 Structured FHIR (intake-extracted)
            </CardTitle>
          </CardHeader>
          <CardContent>
            {c.extracted_facts ? (
              <FhirPanel facts={c.extracted_facts} />
            ) : (
              <p className="text-sm text-muted-foreground">
                Intake hasn't extracted facts yet…
              </p>
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-7">
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <FileText className="h-4 w-4" />
              {policyQ.data ? (
                <>
                  {policyQ.data.name}
                  <Badge variant="outline" className="ml-2">
                    {policyQ.data.policy_id} v{policyQ.data.version}
                  </Badge>
                </>
              ) : (
                "📋 Criteria evaluation"
              )}
            </CardTitle>
          </CardHeader>
          <CardContent>
            {policyQ.isLoading && (
              <p className="text-sm text-muted-foreground">Loading policy…</p>
            )}
            {!policyId && (
              <p className="text-sm text-muted-foreground">
                {inFlight
                  ? "Policy selection pending…"
                  : "No policy was selected for this case."}
              </p>
            )}
            {policyQ.data && c.criteria_evaluation && (
              <CriteriaTree
                policy={policyQ.data}
                leafVerdicts={c.criteria_evaluation.leaf_verdicts}
                exclusionVerdicts={c.criteria_evaluation.exclusion_verdicts}
              />
            )}
            {policyQ.data && !c.criteria_evaluation && (
              <p className="text-sm text-muted-foreground">
                Criteria not yet evaluated.
              </p>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">⚖️ Determination</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {!c.determination ? (
            <p className="text-sm text-muted-foreground">
              {inFlight
                ? "Determination not yet finalized — payer is still reviewing."
                : "No determination available."}
            </p>
          ) : (
            <>
              <div className="flex items-center gap-3 flex-wrap">
                <OutcomeBadge outcome={c.outcome} />
                {c.determination.rationale && (
                  <span className="text-sm text-muted-foreground">
                    {c.determination.rationale}
                  </span>
                )}
              </div>
              {c.determination.triggered_exclusions &&
                c.determination.triggered_exclusions.length > 0 && (
                  <p className="text-sm">
                    <strong>Triggered exclusions:</strong>{" "}
                    {c.determination.triggered_exclusions.join(", ")}
                  </p>
                )}
              {c.determination.narrative && (
                <div>
                  <h4 className="font-semibold mb-1">Reviewer narrative</h4>
                  <p className="text-sm leading-relaxed whitespace-pre-wrap">
                    {c.determination.narrative}
                  </p>
                </div>
              )}
              {c.determination.missing_info &&
                c.determination.missing_info.length > 0 && (
                  <div className="space-y-2">
                    <h4 className="font-semibold">
                      Missing information ({c.determination.missing_info.length})
                    </h4>
                    {c.determination.missing_info.map((mi) => (
                      <div
                        key={mi.id}
                        className="rounded-md border p-3 bg-amber-50"
                      >
                        <p className="text-sm">
                          <strong>{mi.id}</strong>{" "}
                          <span className="text-xs text-muted-foreground">
                            · criterion <code>{mi.criterion_id}</code>
                          </span>
                        </p>
                        <p className="text-sm mt-1">{mi.request}</p>
                      </div>
                    ))}
                  </div>
                )}
            </>
          )}
          <div>
            <h4 className="font-semibold mb-2">Action</h4>
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" disabled>
                ✅ Approve
              </Button>
              <Button variant="outline" disabled>
                ❌ Deny
              </Button>
              <Button variant="outline" disabled>
                📨 Send info request
              </Button>
              <Button variant="outline" disabled>
                👤 Send to medical director
              </Button>
            </div>
            <p className="text-xs text-muted-foreground mt-2">
              UI demo — not wired to a real workflow.
            </p>
          </div>
        </CardContent>
      </Card>

      {c.pas_response_bundle && (
        <Accordion type="single" collapsible>
          <AccordionItem value="bundle" className="border rounded-md">
            <AccordionTrigger className="px-4">
              📤 Raw outbound PAS ClaimResponse Bundle
            </AccordionTrigger>
            <AccordionContent className="px-4">
              <pre className="quote-block max-h-[600px] overflow-auto">
                {JSON.stringify(c.pas_response_bundle, null, 2)}
              </pre>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Live payer progress banner — only shown while the payer is still working.
// ---------------------------------------------------------------------------

function PayerProgressBanner({
  stage,
  errorMessage,
}: {
  stage: ProcessingStage;
  errorMessage: string | null;
}) {
  const info = PAYER_STAGE[stage];

  if (stage === "complete") return null;

  if (stage === "failed") {
    return (
      <Alert variant="destructive">
        <AlertDescription>
          <strong>Payer-side failure.</strong>{" "}
          {errorMessage ?? "Unknown error during processing."}
        </AlertDescription>
      </Alert>
    );
  }

  return (
    <div className="rounded-md border border-violet-200 bg-violet-50/40 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Badge variant="violet">🏥 Payer processing</Badge>
          <Loader2 className="h-4 w-4 animate-spin text-violet-700" />
          <span className="font-medium text-sm">{info.label}</span>
        </div>
        <span className="text-xs text-muted-foreground">{info.pct}%</span>
      </div>
      <Progress value={info.pct} />
      <p className="text-xs text-muted-foreground">
        Streaming live from the payer engine — this page updates the moment
        each stage advances.
      </p>
    </div>
  );
}
