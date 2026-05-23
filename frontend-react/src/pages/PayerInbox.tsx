import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Inbox } from "lucide-react";

import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { listCases } from "@/lib/api";
import type { CaseStatus } from "@/types/api";

const STATUS_LABEL: Record<CaseStatus, { emoji: string; variant: "success" | "danger" | "warning" | "violet" | "muted" }> = {
  approved: { emoji: "🟢", variant: "success" },
  denied: { emoji: "🔴", variant: "danger" },
  pended: { emoji: "🟡", variant: "warning" },
  needs_review: { emoji: "🟣", variant: "violet" },
  processing: { emoji: "⏳", variant: "muted" },
  failed: { emoji: "❌", variant: "danger" },
  unknown: { emoji: "⚪", variant: "muted" },
};

export function PayerInbox() {
  const cases = useQuery({
    queryKey: ["cases"],
    queryFn: listCases,
    refetchInterval: 5_000,
  });

  return (
    <div className="space-y-6">
      <header className="space-y-1.5">
        <h1 className="text-2xl font-bold flex items-center gap-2">
          <Inbox className="h-6 w-6" />
          Inbox — Prior Authorization Cases
        </h1>
        <p className="text-muted-foreground">
          All cases received from providers. Refreshes every 5 seconds.
        </p>
      </header>

      {cases.isError && (
        <Alert variant="destructive">
          <AlertDescription>
            Could not load cases: {cases.error instanceof Error ? cases.error.message : String(cases.error)}
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {cases.data?.length ?? 0} case{cases.data?.length === 1 ? "" : "s"}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {cases.isLoading && (
            <p className="p-5 text-sm text-muted-foreground">Loading…</p>
          )}
          {cases.data && cases.data.length === 0 && (
            <div className="p-5">
              <Alert variant="info">
                <AlertDescription>
                  No cases yet. Either run <code>make seed</code> to ingest the
                  Smith PDF through the doctor flow, or open the Doctor
                  Workspace and upload a patient PDF directly.
                </AlertDescription>
              </Alert>
            </div>
          )}
          {cases.data && cases.data.length > 0 && (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-12"></TableHead>
                  <TableHead>Case ID</TableHead>
                  <TableHead>Patient</TableHead>
                  <TableHead>CPT</TableHead>
                  <TableHead>Payer</TableHead>
                  <TableHead>Policy</TableHead>
                  <TableHead>Branch</TableHead>
                  <TableHead>Outcome</TableHead>
                  <TableHead className="w-12"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {cases.data.map((c) => {
                  const s = STATUS_LABEL[c.status] ?? STATUS_LABEL.unknown;
                  return (
                    <TableRow key={c.case_id}>
                      <TableCell aria-label={c.status} className="text-xl">
                        {s.emoji}
                      </TableCell>
                      <TableCell>
                        <Link
                          to={`/payer/${c.case_id}`}
                          className="font-mono text-xs text-primary hover:underline"
                        >
                          {c.case_id}
                        </Link>
                      </TableCell>
                      <TableCell>{c.patient_display ?? "—"}</TableCell>
                      <TableCell>
                        {c.cpt_code ? (
                          <code className="text-xs">{c.cpt_code}</code>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>{c.payer_id ?? "—"}</TableCell>
                      <TableCell>
                        {c.selected_policy_id ? (
                          <code className="text-xs">{c.selected_policy_id}</code>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>{c.branch ?? "—"}</TableCell>
                      <TableCell>
                        {c.outcome ? (
                          <Badge variant={s.variant}>{c.outcome}</Badge>
                        ) : (
                          "—"
                        )}
                      </TableCell>
                      <TableCell>
                        <Link
                          to={`/payer/${c.case_id}`}
                          className="text-muted-foreground hover:text-primary"
                          aria-label="Open case detail"
                        >
                          <ChevronRight className="h-4 w-4" />
                        </Link>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
