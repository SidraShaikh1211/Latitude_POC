import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import type {
  Citation,
  ExtractedFactItem,
  ExtractedFacts,
} from "@/types/api";

export function FhirPanel({ facts }: { facts: ExtractedFacts | null }) {
  if (!facts) {
    return (
      <p className="text-sm text-muted-foreground">
        No intake-extracted facts (intake may have failed or been skipped).
      </p>
    );
  }
  return (
    <div className="space-y-4">
      <PatientBlock facts={facts} />
      <ResourceList
        title="🩺 Conditions"
        items={facts.conditions}
        primary="display"
        secondary="icd10_code"
      />
      <ResourceList
        title="📊 Observations"
        items={facts.observations}
        primary="code_display"
        secondary="value_string"
      />
      <ResourceList
        title="💊 Medications"
        items={facts.medications}
        primary="medication_name"
        secondary="dose"
      />
      <ResourceList
        title="🛠 Procedures"
        items={facts.procedures}
        primary="display"
        secondary="cpt_code"
      />
      <ResourceList
        title="⚠️ Allergies"
        items={facts.allergies}
        primary="substance"
        secondary="reaction"
      />
      <ResourceList
        title="🧪 Diagnostic Reports"
        items={facts.diagnostic_reports}
        primary="findings"
        secondary="modality"
      />
    </div>
  );
}

function PatientBlock({ facts }: { facts: ExtractedFacts }) {
  const p = facts.patient;
  if (!p) return null;
  return (
    <section>
      <h3 className="font-semibold mb-2">👤 Patient</h3>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-sm mb-2">
        <KV
          k="Name"
          v={`${p.given ?? "?"} ${p.family ?? "?"}`}
        />
        <KV k="DOB" v={p.birth_date ?? "?"} />
        <KV k="Gender" v={p.gender ?? "?"} />
        <KV k="MRN" v={p.mrn ?? "?"} />
      </div>
      <CitationDrawer citations={p.citations} />
    </section>
  );
}

function ResourceList({
  title,
  items,
  primary,
  secondary,
}: {
  title: string;
  items: ExtractedFactItem[] | undefined;
  primary: keyof ExtractedFactItem;
  secondary?: keyof ExtractedFactItem;
}) {
  if (!items || items.length === 0) return null;
  return (
    <section>
      <h3 className="font-semibold mb-2">
        {title} ({items.length})
      </h3>
      <Accordion type="multiple" className="border rounded-md divide-y">
        {items.map((it, i) => {
          const primaryVal = String(it[primary] ?? "?");
          const secondaryVal = secondary
            ? (it[secondary] as string | undefined)
            : undefined;
          const pages = pageChips(it.citations);
          return (
            <AccordionItem
              key={i}
              value={`${title}-${i}`}
              className="border-b-0"
            >
              <AccordionTrigger className="px-3 py-2">
                <div className="flex items-center gap-2 text-left flex-wrap text-sm">
                  <span>{primaryVal}</span>
                  {secondaryVal && (
                    <span className="text-muted-foreground italic">
                      — {secondaryVal}
                    </span>
                  )}
                  {pages.map((p) => (
                    <Badge key={p} variant="muted" className="text-[10px]">
                      p.{p}
                    </Badge>
                  ))}
                </div>
              </AccordionTrigger>
              <AccordionContent className="px-3">
                <CitationDrawer citations={it.citations} />
              </AccordionContent>
            </AccordionItem>
          );
        })}
      </Accordion>
    </section>
  );
}

function CitationDrawer({ citations }: { citations?: Citation[] }) {
  if (!citations || citations.length === 0) {
    return (
      <p className="text-xs italic text-muted-foreground">no citations</p>
    );
  }
  return (
    <ul className="space-y-2 text-sm">
      {citations.map((c, i) => (
        <li key={i}>
          <p className="text-xs text-muted-foreground">
            <strong>p.{c.page}</strong>
            {" · "}
            {c.section ?? "unsectioned"}
            {typeof c.extraction_confidence === "number" &&
              ` · conf=${c.extraction_confidence.toFixed(2)}`}
          </p>
          <pre className="quote-block">{c.quote}</pre>
        </li>
      ))}
    </ul>
  );
}

function pageChips(cits: Citation[] | undefined): number[] {
  if (!cits) return [];
  const s = new Set<number>();
  cits.forEach((c) => c.page && s.add(c.page));
  return [...s].sort((a, b) => a - b);
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <p>
      <span className="text-muted-foreground">{k}:</span>{" "}
      <span className="font-medium">{v}</span>
    </p>
  );
}
