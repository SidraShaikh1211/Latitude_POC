import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Loader2 } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { OutcomeBadge } from "@/components/OutcomeBadge";
import { listMetricsRuns } from "@/lib/api";
import type {
  AdjudicationLeafMetric,
  MetricsStage,
  PerformanceRun,
} from "@/types/api";

// ---------------------------------------------------------------------------
// Formatters
// ---------------------------------------------------------------------------

function fmtDuration(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)} ms`;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const m = Math.floor(seconds / 60);
  const s = (seconds - m * 60).toFixed(0);
  return `${m}m ${s}s`;
}

function fmtTokens(n: number): string {
  if (!n) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)} M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)} K`;
  return String(n);
}

function fmtCost(usd: number): string {
  if (!usd) return "$0";
  if (usd < 0.01) return `$${usd.toFixed(4)}`;
  return `$${usd.toFixed(3)}`;
}

function fmtPct(rate: number): string {
  return `${(rate * 100).toFixed(0)}%`;
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export function Performance() {
  const runs = useQuery({
    queryKey: ["metrics-runs"],
    queryFn: listMetricsRuns,
    refetchInterval: 5_000,
  });

  return (
    <div className="space-y-6">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          📊 Performance
        </h1>
        <p className="text-muted-foreground max-w-3xl">
          Per-run telemetry across the full PA pipeline. The doctor side
          contributes the metadata-extraction call; the payer side contributes
          intake, per-criterion adjudication, and the reviewer. Click any row
          to see the per-stage breakdown and per-leaf adjudication cost.
        </p>
      </header>

      {runs.isLoading && (
        <p className="text-muted-foreground flex items-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading runs…
        </p>
      )}
      {runs.isError && (
        <Alert variant="destructive">
          <AlertDescription>
            Couldn't load performance metrics:{" "}
            {runs.error instanceof Error
              ? runs.error.message
              : String(runs.error)}
          </AlertDescription>
        </Alert>
      )}

      {runs.data && runs.data.length === 0 && (
        <Alert variant="info">
          <AlertDescription>
            No runs yet. Submit a PDF on the Doctor page to populate this view.
          </AlertDescription>
        </Alert>
      )}

      {runs.data && runs.data.length > 0 && (
        <>
          <AggregateCards runs={runs.data} />
          <RunsTable runs={runs.data} />
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Top-line aggregates (so you can compare runs at a glance)
// ---------------------------------------------------------------------------

function AggregateCards({ runs }: { runs: PerformanceRun[] }) {
  const finished = runs.filter((r) => r.totals.llm_calls > 0);
  const n = finished.length || 1;
  const avgDuration =
    finished.reduce((acc, r) => acc + r.duration_seconds, 0) / n;
  const avgCost = finished.reduce((acc, r) => acc + r.totals.cost_usd, 0) / n;
  const avgTokens =
    finished.reduce(
      (acc, r) =>
        acc +
        r.totals.tokens_in +
        r.totals.tokens_out +
        r.totals.cache_read +
        r.totals.cache_creation,
      0,
    ) / n;
  const avgCalls =
    finished.reduce((acc, r) => acc + r.totals.llm_calls, 0) / n;

  return (
    <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-3">
      <AggCard label="Runs (with LLM)" value={String(finished.length)} hint={`of ${runs.length} total`} />
      <AggCard label="Avg duration" value={fmtDuration(avgDuration)} />
      <AggCard label="Avg cost" value={fmtCost(avgCost)} />
      <AggCard label="Avg tokens · LLM calls" value={`${fmtTokens(avgTokens)} · ${avgCalls.toFixed(1)}`} />
    </div>
  );
}

function AggCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription className="text-xs">{label}</CardDescription>
        <CardTitle className="text-xl">{value}</CardTitle>
      </CardHeader>
      {hint && (
        <CardContent className="pt-0 text-xs text-muted-foreground">
          {hint}
        </CardContent>
      )}
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Runs table — one row per run, click to expand for per-stage + per-leaf
// ---------------------------------------------------------------------------

function RunsTable({ runs }: { runs: PerformanceRun[] }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  function toggle(id: string) {
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Recent runs</CardTitle>
        <CardDescription>
          One row per submission. Joined doctor↔payer via{" "}
          <code>Submission.payer_case_id</code>.
        </CardDescription>
      </CardHeader>
      <CardContent className="p-0">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-8"></TableHead>
              <TableHead>Run</TableHead>
              <TableHead>Outcome</TableHead>
              <TableHead className="text-right">Duration</TableHead>
              <TableHead className="text-right">Tokens in</TableHead>
              <TableHead className="text-right">Tokens out</TableHead>
              <TableHead className="text-right" title="Cache reads (cheaper)">
                Cache rd
              </TableHead>
              <TableHead className="text-right" title="Cache writes (slightly pricier than fresh)">
                Cache cr
              </TableHead>
              <TableHead className="text-right">LLM calls</TableHead>
              <TableHead className="text-right">Cost</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {runs.map((r) => {
              const isOpen = expanded.has(r.submission_id);
              return (
                <RunRowGroup
                  key={r.submission_id}
                  run={r}
                  isOpen={isOpen}
                  onToggle={() => toggle(r.submission_id)}
                />
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function RunRowGroup({
  run,
  isOpen,
  onToggle,
}: {
  run: PerformanceRun;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const meta: string[] = [];
  if (run.patient_display) meta.push(run.patient_display);
  if (run.cpt_code) meta.push(`CPT ${run.cpt_code}`);
  if (run.selected_policy_id) meta.push(run.selected_policy_id);

  return (
    <>
      <TableRow
        onClick={onToggle}
        className="cursor-pointer hover:bg-muted/50"
      >
        <TableCell>
          {isOpen ? (
            <ChevronDown className="h-4 w-4" />
          ) : (
            <ChevronRight className="h-4 w-4" />
          )}
        </TableCell>
        <TableCell>
          <div className="flex flex-col">
            <code className="text-xs text-primary">{run.submission_id}</code>
            {meta.length > 0 && (
              <span className="text-xs text-muted-foreground">
                {meta.join(" · ")}
              </span>
            )}
          </div>
        </TableCell>
        <TableCell>
          <div className="flex items-center gap-2">
            <OutcomeBadge outcome={run.outcome} />
            {run.case_status && run.case_status !== "approved" &&
              run.case_status !== "denied" && run.case_status !== "pended" &&
              run.case_status !== "needs_review" && (
                <Badge variant="muted">{run.case_status}</Badge>
              )}
          </div>
        </TableCell>
        <TableCell className="text-right font-mono text-sm">
          {fmtDuration(run.duration_seconds)}
        </TableCell>
        <TableCell className="text-right font-mono text-sm">
          {fmtTokens(run.totals.tokens_in)}
        </TableCell>
        <TableCell className="text-right font-mono text-sm">
          {fmtTokens(run.totals.tokens_out)}
        </TableCell>
        <TableCell className="text-right font-mono text-sm text-emerald-700">
          {fmtTokens(run.totals.cache_read)}
        </TableCell>
        <TableCell className="text-right font-mono text-sm">
          {fmtTokens(run.totals.cache_creation)}
        </TableCell>
        <TableCell className="text-right font-mono text-sm">
          {run.totals.llm_calls || "—"}
        </TableCell>
        <TableCell className="text-right font-mono text-sm font-semibold">
          {fmtCost(run.totals.cost_usd)}
        </TableCell>
      </TableRow>
      {isOpen && (
        <TableRow className="bg-muted/30 hover:bg-muted/30">
          <TableCell colSpan={10} className="p-4">
            <RunExpansion run={run} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Per-run drill-down (stages + per-leaf adjudication)
// ---------------------------------------------------------------------------

function RunExpansion({ run }: { run: PerformanceRun }) {
  return (
    <div className="space-y-6">
      <div className="flex flex-wrap gap-x-6 gap-y-2 text-xs text-muted-foreground">
        <span>
          <strong className="text-foreground">Doctor leg:</strong>{" "}
          {fmtDuration(run.doctor_duration_seconds)}
        </span>
        <span>
          <strong className="text-foreground">Payer leg:</strong>{" "}
          {fmtDuration(run.payer_duration_seconds)}
        </span>
        <span>
          <strong className="text-foreground">Cache hit rate:</strong>{" "}
          {fmtPct(run.totals.cache_hit_rate)}
        </span>
        {run.payer_case_id && (
          <span>
            <strong className="text-foreground">Payer case:</strong>{" "}
            <code>{run.payer_case_id}</code>
          </span>
        )}
      </div>

      <StagesTable stages={run.stages} />

      {run.adjudication_leaves.length > 0 && (
        <LeavesTable leaves={run.adjudication_leaves} />
      )}
    </div>
  );
}

function StagesTable({ stages }: { stages: MetricsStage[] }) {
  if (stages.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No stage telemetry recorded yet for this run.
      </p>
    );
  }
  return (
    <div>
      <h4 className="text-sm font-semibold mb-2">Pipeline stages</h4>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Stage</TableHead>
            <TableHead>Side</TableHead>
            <TableHead className="text-right">Duration</TableHead>
            <TableHead className="text-right">LLM calls</TableHead>
            <TableHead className="text-right">Tokens in</TableHead>
            <TableHead className="text-right">Tokens out</TableHead>
            <TableHead className="text-right">Cache rd</TableHead>
            <TableHead className="text-right">Cache cr</TableHead>
            <TableHead className="text-right">Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {stages.map((s, i) => (
            <TableRow key={`${s.name}-${i}`}>
              <TableCell className="font-medium">{s.name}</TableCell>
              <TableCell>
                <Badge variant={s.side === "doctor" ? "info" : "violet"}>
                  {s.side ?? "payer"}
                </Badge>
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtDuration(s.duration_seconds)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {s.llm_calls || "—"}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtTokens(s.tokens_in)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtTokens(s.tokens_out)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-emerald-700">
                {fmtTokens(s.cache_read)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtTokens(s.cache_creation)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtCost(s.cost_usd)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function LeavesTable({ leaves }: { leaves: AdjudicationLeafMetric[] }) {
  // Sort by cost desc so the expensive criteria float to the top.
  const sorted = [...leaves].sort((a, b) => b.cost_usd - a.cost_usd);
  return (
    <div>
      <h4 className="text-sm font-semibold mb-2">
        Per-criterion adjudication ({leaves.length})
      </h4>
      <p className="text-xs text-muted-foreground mb-2">
        <code>iterations = 0</code> means the deterministic short-circuit
        resolved the criterion without an LLM call.
      </p>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Criterion</TableHead>
            <TableHead>Kind</TableHead>
            <TableHead>Verdict</TableHead>
            <TableHead className="text-right">LLM iters</TableHead>
            <TableHead className="text-right">Tokens in</TableHead>
            <TableHead className="text-right">Tokens out</TableHead>
            <TableHead className="text-right">Cache rd</TableHead>
            <TableHead className="text-right">Cost</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {sorted.map((l) => (
            <TableRow key={`${l.kind}-${l.criterion_id}`}>
              <TableCell>
                <code className="text-xs">{l.criterion_id}</code>
              </TableCell>
              <TableCell>
                <Badge variant={l.kind === "exclusion" ? "warning" : "muted"}>
                  {l.kind}
                </Badge>
              </TableCell>
              <TableCell>
                <span className="text-xs">{l.verdict ?? "—"}</span>
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {l.iterations}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtTokens(l.tokens_in)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtTokens(l.tokens_out)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm text-emerald-700">
                {fmtTokens(l.cache_read)}
              </TableCell>
              <TableCell className="text-right font-mono text-sm">
                {fmtCost(l.cost_usd)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
