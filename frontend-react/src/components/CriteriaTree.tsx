import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type {
  CriterionNode,
  LeafVerdict,
  PolicyDetail,
  Verdict,
} from "@/types/api";

const VERDICT_STYLE: Record<
  Verdict,
  { emoji: string; variant: "success" | "danger" | "warning" | "muted" }
> = {
  met: { emoji: "✅", variant: "success" },
  not_met: { emoji: "❌", variant: "danger" },
  unclear: { emoji: "🟡", variant: "warning" },
  not_documented: { emoji: "📭", variant: "muted" },
};

export function CriteriaTree({
  policy,
  leafVerdicts,
  exclusionVerdicts,
}: {
  policy: PolicyDetail;
  leafVerdicts: Record<string, LeafVerdict>;
  exclusionVerdicts: Record<string, LeafVerdict>;
}) {
  return (
    <div className="space-y-6">
      <div>
        <h3 className="font-semibold mb-2">Criteria tree</h3>
        <div className="space-y-1">
          <TreeNode node={policy.criteria} depth={0} verdicts={leafVerdicts} />
        </div>
      </div>
      {policy.exclusions.length > 0 && (
        <div>
          <h3 className="font-semibold mb-2">Exclusions</h3>
          <Accordion type="multiple" className="border rounded-md divide-y">
            {policy.exclusions.map((ex) => {
              const v = exclusionVerdicts[ex.id];
              const verdict = (v?.verdict ?? "not_documented") as Verdict;
              const style = VERDICT_STYLE[verdict];
              return (
                <AccordionItem
                  key={ex.id}
                  value={ex.id}
                  className="border-b-0"
                >
                  <AccordionTrigger className="px-3">
                    <div className="flex items-center gap-2 text-left flex-wrap">
                      <Badge variant={style.variant}>
                        {style.emoji} {verdict}
                      </Badge>
                      <code className="text-xs">{ex.id}</code>
                      <span className="text-xs text-muted-foreground line-clamp-1">
                        {ex.description.slice(0, 80)}
                      </span>
                    </div>
                  </AccordionTrigger>
                  <AccordionContent className="px-3 space-y-2">
                    <CitationBlock
                      page={ex.policy_citation.page}
                      section={ex.policy_citation.section}
                      quote={ex.policy_citation.quote}
                    />
                    {v?.reasoning && (
                      <>
                        <h5 className="text-xs font-semibold mt-2">
                          Adjudicator reasoning
                        </h5>
                        <p className="text-sm leading-relaxed">
                          {v.reasoning}
                        </p>
                      </>
                    )}
                  </AccordionContent>
                </AccordionItem>
              );
            })}
          </Accordion>
        </div>
      )}
    </div>
  );
}

function TreeNode({
  node,
  depth,
  verdicts,
}: {
  node: CriterionNode;
  depth: number;
  verdicts: Record<string, LeafVerdict>;
}) {
  const indent = { paddingLeft: `${depth * 16}px` };

  if (node.type === "internal") {
    return (
      <div className="space-y-1">
        <div className="text-sm" style={indent}>
          <Badge variant="outline" className="font-mono text-[10px]">
            {node.operator}
          </Badge>{" "}
          <span className="font-medium">{tail(node.id)}</span>
          <span className="text-muted-foreground"> — {node.description}</span>
        </div>
        {(node.children ?? []).map((c) => (
          <TreeNode
            key={c.id}
            node={c}
            depth={depth + 1}
            verdicts={verdicts}
          />
        ))}
      </div>
    );
  }

  const v = verdicts[node.id];
  const verdict = (v?.verdict ?? "not_documented") as Verdict;
  const style = VERDICT_STYLE[verdict];

  return (
    <div style={indent}>
      <Accordion type="single" collapsible>
        <AccordionItem value={node.id} className="border rounded-md">
          <AccordionTrigger
            className={cn(
              "px-3 py-2",
              verdict === "not_met" && "bg-red-50/50",
              verdict === "unclear" && "bg-amber-50/50",
              verdict === "met" && "bg-emerald-50/40",
            )}
          >
            <div className="flex items-center gap-2 text-left flex-wrap">
              <Badge variant={style.variant}>
                {style.emoji} {verdict}
              </Badge>
              {typeof v?.confidence === "number" && (
                <span className="text-xs text-muted-foreground">
                  conf {v.confidence.toFixed(2)}
                </span>
              )}
              <code className="text-xs">{tail(node.id)}</code>
            </div>
          </AccordionTrigger>
          <AccordionContent className="px-3 space-y-3">
            <p className="text-sm italic text-muted-foreground">
              {node.description}
            </p>
            {node.policy_citation && (
              <CitationBlock
                page={node.policy_citation.page}
                section={node.policy_citation.section}
                quote={node.policy_citation.quote}
              />
            )}
            {v?.reasoning && (
              <div>
                <h5 className="text-xs font-semibold mb-1">Reasoning</h5>
                <p className="text-sm leading-relaxed">{v.reasoning}</p>
              </div>
            )}
            {v?.patient_evidence && v.patient_evidence.length > 0 && (
              <div>
                <h5 className="text-xs font-semibold mb-1">
                  Patient evidence cited
                </h5>
                <ul className="space-y-2">
                  {v.patient_evidence.map((e, i) => (
                    <li key={i} className="text-sm">
                      <span className="italic text-muted-foreground">
                        {e.fact_type ?? "unknown"}
                      </span>{" "}
                      · {e.value_summary ?? ""}{" "}
                      {e.page && (
                        <Badge variant="muted" className="text-[10px]">
                          p.{e.page}
                        </Badge>
                      )}
                      {e.quote && (
                        <pre className="quote-block mt-1">{e.quote}</pre>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {v?.missing_info && v.missing_info.length > 0 && (
              <div>
                <h5 className="text-xs font-semibold mb-1">Missing info</h5>
                <ul className="text-sm list-disc pl-5 space-y-0.5">
                  {v.missing_info.map((m, i) => (
                    <li key={i}>{m}</li>
                  ))}
                </ul>
              </div>
            )}
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

function CitationBlock({
  page,
  section,
  quote,
}: {
  page: number;
  section: string | null;
  quote: string;
}) {
  return (
    <div>
      <h5 className="text-xs font-semibold mb-1">
        Policy citation (p.{page}, {section ?? "unsectioned"})
      </h5>
      <pre className="quote-block">{quote}</pre>
    </div>
  );
}

function tail(id: string): string {
  const parts = id.split(".");
  return parts[parts.length - 1] ?? id;
}
