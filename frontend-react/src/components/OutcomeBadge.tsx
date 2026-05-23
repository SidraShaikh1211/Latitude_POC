import { Badge } from "@/components/ui/badge";
import type { Outcome } from "@/types/api";

const map: Record<
  Outcome,
  { label: string; variant: "success" | "danger" | "warning" | "violet" }
> = {
  approve: { label: "🟢 APPROVED", variant: "success" },
  deny: { label: "🔴 DENIED", variant: "danger" },
  pend: { label: "🟡 PENDED — info requested", variant: "warning" },
  needs_human_review: {
    label: "🟣 SENT TO HUMAN REVIEW",
    variant: "violet",
  },
};

export function OutcomeBadge({ outcome }: { outcome: Outcome | null }) {
  if (!outcome) return <Badge variant="muted">unknown</Badge>;
  const e = map[outcome];
  return (
    <Badge variant={e.variant} className="text-sm py-1 px-3">
      {e.label}
    </Badge>
  );
}
