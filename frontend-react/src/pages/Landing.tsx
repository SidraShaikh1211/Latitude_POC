import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Inbox, Stethoscope } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { listPolicies } from "@/lib/api";

export function Landing() {
  const policies = useQuery({
    queryKey: ["policies"],
    queryFn: listPolicies,
  });

  return (
    <div className="space-y-10">
      <section>
        <h1 className="text-3xl font-bold tracking-tight">
          PA Prototype — Prior Authorization Prototype
        </h1>
        <p className="mt-3 text-muted-foreground max-w-3xl">
          Payer-side decision support with a simulated provider front door. Da
          Vinci PAS Bundles in, ClaimResponse Bundles out. Citation-grounded
          FHIR extraction, deterministic policy selection, per-criterion
          adjudication, narrative review.
        </p>
      </section>

      <section className="grid md:grid-cols-2 gap-5">
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Stethoscope className="h-5 w-5 text-primary" />
              <CardTitle>Doctor</CardTitle>
            </div>
            <CardDescription>
              Submit a new prior-auth request. Upload a patient's clinical PDF
              — no form fields. Watch the payer process it live, receive the
              determination + missing-info requests.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <ul className="text-sm text-muted-foreground list-disc pl-5 space-y-1">
              <li>Drop any clinical PDF (fax bundle, EMR export, H&amp;P)</li>
              <li>Metadata extractor reads CPT/ICD-10/patient info</li>
              <li>FHIR Bundle assembled and sent to the payer</li>
              <li>Live status updates every 2 seconds</li>
            </ul>
            <Button asChild>
              <Link to="/doctor">
                Open Doctor Workspace <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Inbox className="h-5 w-5 text-primary" />
              <CardTitle>Payer (Utilization Management)</CardTitle>
            </div>
            <CardDescription>
              Review incoming PA submissions. See the full criteria tree with
              per-leaf verdicts, the Reviewer's narrative, and the outbound
              PAS ClaimResponse Bundle.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <ul className="text-sm text-muted-foreground list-disc pl-5 space-y-1">
              <li>Inbox of all received cases</li>
              <li>Three-panel workspace: FHIR · Criteria · Determination</li>
              <li>Click any criterion for evidence + reasoning + quote</li>
              <li>Same backend as the doctor surface</li>
            </ul>
            <Button asChild variant="outline">
              <Link to="/payer">
                Open Payer Inbox <ArrowRight className="h-4 w-4" />
              </Link>
            </Button>
          </CardContent>
        </Card>
      </section>

      <section className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 className="text-xl font-semibold">Loaded policies</h2>
          <p className="text-sm text-muted-foreground">
            The selector matches incoming requests against{" "}
            <code>applies_to</code>.
          </p>
        </div>
        <Card>
          <CardContent className="pt-5">
            {policies.isLoading && (
              <p className="text-sm text-muted-foreground">Loading…</p>
            )}
            {policies.isError && (
              <p className="text-sm text-destructive">
                Could not load policies — is the API running?
              </p>
            )}
            {policies.data && policies.data.length === 0 && (
              <p className="text-sm text-muted-foreground">
                No policies loaded.
              </p>
            )}
            {policies.data && policies.data.length > 0 && (
              <Accordion type="multiple" className="w-full">
                {policies.data.map((p) => (
                  <AccordionItem key={p.policy_id} value={p.policy_id}>
                    <AccordionTrigger>
                      <div className="flex flex-wrap items-center gap-2">
                        <span>{p.name}</span>
                        <Badge variant="outline">{p.policy_id}</Badge>
                        <Badge variant="muted">v{p.version}</Badge>
                      </div>
                    </AccordionTrigger>
                    <AccordionContent>
                      <div className="grid md:grid-cols-3 gap-4 mb-3">
                        <Metric label="Tree leaves" value={p.leaf_count} />
                        <Metric
                          label="Exclusions"
                          value={p.exclusion_count}
                        />
                        <Metric
                          label="Covered CPTs"
                          value={p.cpt_codes.length}
                        />
                      </div>
                      <dl className="text-sm grid sm:grid-cols-2 gap-y-1.5 gap-x-6">
                        <Row k="Payer" v={p.payer_id} />
                        <Row k="LOB" v={p.lines_of_business.join(", ")} />
                        <Row k="States" v={p.states.join(", ")} />
                        <Row
                          k="Effective"
                          v={`${p.effective_from} → ${
                            p.effective_until ?? "present"
                          }`}
                        />
                        <Row
                          k="CPTs"
                          v={p.cpt_codes.join(", ")}
                          span
                        />
                      </dl>
                    </AccordionContent>
                  </AccordionItem>
                ))}
              </Accordion>
            )}
          </CardContent>
        </Card>
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md border bg-muted/30 p-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="text-2xl font-semibold mt-0.5">{value}</div>
    </div>
  );
}

function Row({ k, v, span }: { k: string; v: string; span?: boolean }) {
  return (
    <div className={span ? "sm:col-span-2" : ""}>
      <span className="text-muted-foreground">{k}:</span>{" "}
      <span className="font-medium">{v}</span>
    </div>
  );
}
