import { Link, NavLink, Outlet } from "react-router-dom";
import { Activity, Bell, Home, PlayCircle, Settings2, Workflow } from "lucide-react";
import { CommandStatusStrip } from "@/components/common/command-status-strip";
import { cn } from "@/lib/utils";

const navItems = [
  { to: "/", label: "Home", icon: Home },
  { to: "/workflows", label: "Workflows", icon: Workflow },
  { to: "/runs", label: "Runs", icon: PlayCircle },
  { to: "/alerts", label: "Alerts", icon: Bell },
  { to: "/settings", label: "Settings", icon: Settings2 },
  { to: "/observability", label: "Observability", icon: Activity }
];

export function AppShell(): JSX.Element {
  return (
    <div className="min-h-screen pb-5">
      <header className="sticky top-0 z-30 border-b border-border/80 bg-background/96 backdrop-blur supports-[backdrop-filter]:bg-background/92">
        <div className="container flex flex-wrap items-center justify-between gap-4 py-3.5">
          <div className="space-y-0.5">
            <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-primary">Operations Platform</div>
            <Link to="/" className="text-[1.28rem] font-semibold tracking-tight">
              Mission Control
            </Link>
          </div>
          <div className="rounded-full border border-border/80 bg-card/85 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.08em] text-muted-foreground shadow-[inset_0_1px_0_rgba(255,255,255,0.65)]">
            Operator Console
          </div>
        </div>
        <CommandStatusStrip />
      </header>

      <div className="container mt-3.5 grid gap-4 md:grid-cols-[248px_minmax(0,1fr)]">
        <aside className="h-fit rounded-xl border border-border/80 bg-card/90 p-2 shadow-[0_1px_2px_rgba(16,24,40,0.06)] md:sticky md:top-[124px]">
          <div className="px-2 pb-2 pt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">Navigation</div>
          <nav className="grid gap-1.5">
            {navItems.map((item) => {
              const Icon = item.icon;
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.to === "/"}
                  className={({ isActive }) =>
                    cn(
                      "group relative flex items-center gap-2 rounded-lg border px-3 py-2 text-[13px] font-medium transition-colors",
                      isActive
                        ? "border-primary/30 bg-primary/10 text-primary shadow-[inset_0_1px_0_rgba(255,255,255,0.65)]"
                        : "border-transparent text-foreground hover:border-border/80 hover:bg-muted/55"
                    )
                  }
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  <span>{item.label}</span>
                </NavLink>
              );
            })}
          </nav>
        </aside>
        <main className="space-y-4 pb-2">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
