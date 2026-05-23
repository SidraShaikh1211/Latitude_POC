import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import type { ExtractedMetadata } from "@/types/api";

export function ExtractedMetadataPanel({
  metadata,
  notes,
}: {
  metadata: ExtractedMetadata | undefined;
  notes?: string;
}) {
  if (!metadata) return null;
  const p = metadata.patient ?? {};
  const c = metadata.coverage ?? {};
  const s = metadata.service_request ?? {};
  return (
    <Accordion type="single" collapsible>
      <AccordionItem value="metadata">
        <AccordionTrigger>📋 What we extracted from the PDF</AccordionTrigger>
        <AccordionContent>
          <div className="grid md:grid-cols-2 gap-6 text-sm">
            <div className="space-y-1">
              <SectionTitle>Patient</SectionTitle>
              <KV k="Name" v={`${p.patient_given ?? "?"} ${p.patient_family ?? "?"}`} />
              <KV k="DOB" v={p.patient_dob ?? "?"} />
              <KV k="Gender" v={p.patient_gender ?? "?"} />
              <KV k="State" v={p.patient_state ?? "?"} />
              <SectionTitle className="pt-3">Coverage</SectionTitle>
              <KV
                k="Payer"
                v={`${c.payer_id ?? "?"} (${c.payer_display ?? "?"})`}
              />
              <KV k="Member ID" v={c.member_id ?? "?"} />
              <KV k="LOB" v={c.line_of_business ?? "?"} />
              <KV k="Plan" v={c.plan_name ?? "?"} />
            </div>
            <div className="space-y-1">
              <SectionTitle>Service requested</SectionTitle>
              <KV k="CPT" v={s.cpt_code ?? "?"} />
              <p className="text-muted-foreground italic pl-1">
                {s.cpt_display ?? ""}
              </p>
              <KV k="DOS" v={s.service_date ?? "?"} />
              <KV k="Body site" v={s.body_site_display ?? "?"} />
              <SectionTitle className="pt-3">ICD-10</SectionTitle>
              <ul className="space-y-0.5">
                {(s.icd10_codes ?? []).map((icd) => (
                  <li key={icd.code}>
                    <code className="px-1.5 py-0.5 rounded bg-muted text-xs">
                      {icd.code}
                    </code>{" "}
                    <span className="text-muted-foreground">({icd.kind})</span> —{" "}
                    {icd.display}
                  </li>
                ))}
              </ul>
            </div>
          </div>
          {notes && (
            <div className="mt-4 text-xs">
              <SectionTitle>Extraction reasoning</SectionTitle>
              <p className="text-muted-foreground whitespace-pre-wrap">{notes}</p>
            </div>
          )}
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  );
}

function SectionTitle({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <h4 className={`font-semibold text-foreground ${className}`}>{children}</h4>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <p>
      <span className="text-muted-foreground">{k}:</span>{" "}
      <code className="px-1.5 py-0.5 rounded bg-muted text-xs">{v}</code>
    </p>
  );
}
