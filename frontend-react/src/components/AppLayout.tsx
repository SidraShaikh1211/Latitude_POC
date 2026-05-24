import { Link, NavLink, Outlet } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Activity, Gauge, Hospital, Inbox, Stethoscope } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { API_BASE, getHealth } from "@/lib/api";

function HealthBadge() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["health"],
    queryFn: getHealth,
    refetchInterval: 30_000,
  });

  if (isLoading) {
    return <Badge variant="muted">checking API…</Badge>;
  }
  if (isError || data?.status !== "ok") {
    return (
      <Badge variant="danger" title="Start the backend with `make run`.">
        API unreachable
      </Badge>
    );
  }
  return (
    <Badge variant="success" title={`API: ${API_BASE}`}>
      API connected
    </Badge>
  );
}

const navItems = [
  { to: "/", label: "Home", icon: Activity, end: true },
  { to: "/doctor", label: "Doctor", icon: Stethoscope },
  { to: "/payer", label: "Payer Inbox", icon: Inbox },
  { to: "/performance", label: "Performance", icon: Gauge },
];

export function AppLayout() {
  return (
    <div className="min-h-screen flex flex-col">
      <header className="border-b bg-white sticky top-0 z-30">
        <div className="container mx-auto px-6 py-3 flex items-center gap-6">
          <Link to="/" className="flex items-center gap-2 font-semibold">
            <Hospital className="h-5 w-5 text-primary" />
            PA Prototype — PA
          </Link>
          <nav className="flex items-center gap-1 ml-4">
            {navItems.map((it) => {
              const Icon = it.icon;
              return (
                <NavLink
                  key={it.to}
                  to={it.to}
                  end={it.end}
                  className={({ isActive }) =>
                    cn(
                      "inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium",
                      isActive
                        ? "bg-primary/10 text-primary"
                        : "text-muted-foreground hover:bg-accent hover:text-foreground",
                    )
                  }
                >
                  <Icon className="h-4 w-4" />
                  {it.label}
                </NavLink>
              );
            })}
          </nav>
          <div className="ml-auto">
            <HealthBadge />
          </div>
        </div>
      </header>
      <main className="container mx-auto px-6 py-8 flex-1">
        <Outlet />
      </main>
      <footer className="border-t py-4 text-xs text-muted-foreground text-center">
        Da Vinci PAS Bundles in · ClaimResponse Bundles out · React UI on
        <code className="mx-1">:5173</code> · FastAPI on
        <code className="mx-1">:8000</code>
      </footer>
    </div>
  );
}
