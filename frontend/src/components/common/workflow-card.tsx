import type { ReactNode } from "react";
import { AlertTriangle, CheckCircle2, Play, Settings2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { StatusBadge } from "@/components/common/status-badge";
import { cn } from "@/lib/utils";

export function WorkflowCard({
  name,
  taskType,
  status,
  cadence,
  description,
  stateMessage,
  recentRunMessage,
  lastRunStatus,
  lastRunAt,
  lastResultSummary,
  nextLikelyAction,
  notificationBehavior,
  errorMessage,
  onRun,
  onConfigure,
  actions
}: {
  name: string;
  taskType: string;
  status: string;
  cadence?: string;
  description?: string;
  stateMessage?: string;
  recentRunMessage?: string;
  lastRunStatus?: string | null;
  lastRunAt?: string | null;
  lastResultSummary?: string;
  nextLikelyAction?: string;
  notificationBehavior?: string | null;
  errorMessage?: string | null;
  onRun?: () => void;
  onConfigure?: () => void;
  actions?: ReactNode;
}): JSX.Element {
  const normalizedStatus = String(status || "").toLowerCase();
  const normalizedLastRunStatus = String(lastRunStatus || "").toLowerCase();
  const isActive = ["enabled", "running", "live", "ready", "success"].includes(normalizedStatus);
  const hasWarningState = ["paused", "awaiting", "manual"].includes(normalizedStatus);
  const hasErrorState = ["disabled", "failed", "failed_permanent", "blocked_budget", "error"].includes(normalizedStatus);
  const lastRunSucceeded = ["success", "succeeded"].includes(normalizedLastRunStatus);
  const lastRunFailed = ["failed", "failed_permanent", "blocked_budget", "error"].includes(normalizedLastRunStatus);

  return (
    <Card
      className={cn(
        "border-border/80",
        isActive && "border-success/35 bg-success/5",
        hasWarningState && "border-warning/30 bg-warning/5",
        hasErrorState && "border-destructive/35 bg-destructive/5"
      )}
    >
      <CardHeader className="pb-1.5">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-[13px]">{name}</CardTitle>
          <StatusBadge status={status} />
        </div>
      </CardHeader>
      <CardContent className="space-y-2.5 text-sm">
        <div className="font-mono text-[11px] text-muted-foreground">{taskType}</div>
        {description ? <p className="text-[12px] leading-relaxed text-muted-foreground">{description}</p> : null}
        {stateMessage ? <div className="text-[12px] leading-relaxed text-muted-foreground">{stateMessage}</div> : null}
        <div className="space-y-1 rounded-md border border-border/70 bg-muted/15 px-2 py-2 text-[11px] leading-relaxed text-muted-foreground break-words">
          <div>
            <span className="font-semibold uppercase tracking-[0.06em] text-foreground">state:</span> {status.replace(/_/g, " ")}
          </div>
          {cadence ? (
            <div>
              <span className="font-semibold uppercase tracking-[0.06em] text-foreground">effective interval:</span> {cadence}
            </div>
          ) : null}
          {lastRunAt ? (
            <div>
              <span className="font-semibold uppercase tracking-[0.06em] text-foreground">last run:</span> {lastRunAt}
              {lastRunStatus ? ` (${String(lastRunStatus).replace(/_/g, " ")})` : ""}
            </div>
          ) : (
            <div>
              <span className="font-semibold uppercase tracking-[0.06em] text-foreground">last run:</span> not yet
            </div>
          )}
          {lastResultSummary ? (
            <div>
              <span className="font-semibold uppercase tracking-[0.06em] text-foreground">last result:</span> {lastResultSummary}
            </div>
          ) : null}
          {notificationBehavior ? (
            <div>
              <span className="font-semibold uppercase tracking-[0.06em] text-foreground">notifications:</span> {notificationBehavior}
            </div>
          ) : null}
          {nextLikelyAction ? (
            <div>
              <span className="font-semibold uppercase tracking-[0.06em] text-foreground">next likely action:</span> {nextLikelyAction}
            </div>
          ) : null}
        </div>
        {recentRunMessage ? (
          <div
            className={cn(
              "rounded-md border px-2 py-1.5 text-[11px] leading-relaxed",
              lastRunSucceeded
                ? "border-success/30 bg-success/10 text-success"
                : lastRunFailed
                  ? "border-destructive/35 bg-destructive/10 text-destructive"
                  : "border-border/70 bg-muted/30 text-muted-foreground"
            )}
          >
            <div className="flex items-center gap-1.5">
              {lastRunSucceeded ? <CheckCircle2 className="h-3.5 w-3.5" /> : null}
              {lastRunFailed ? <AlertTriangle className="h-3.5 w-3.5" /> : null}
              <span className="font-medium uppercase tracking-[0.06em]">
                Last run {lastRunStatus ? String(lastRunStatus).replace(/_/g, " ") : "status"}
              </span>
            </div>
            <div className={cn("mt-1", lastRunSucceeded || lastRunFailed ? "text-current" : "text-muted-foreground")}>{recentRunMessage}</div>
          </div>
        ) : null}
        {errorMessage ? <div className="rounded-md border border-destructive/35 bg-destructive/10 px-2 py-1.5 text-[11px] leading-relaxed text-destructive">{errorMessage}</div> : null}
        {cadence ? (
          <div className="text-[11px] text-muted-foreground">
            cadence: <span className="font-medium text-foreground">{cadence}</span>
          </div>
        ) : null}
        <div className="flex flex-wrap items-center gap-2 pt-0.5">
          {onRun ? (
            <Button size="sm" onClick={onRun}>
              <Play className="h-3.5 w-3.5" />
              Run
            </Button>
          ) : null}
          {onConfigure ? (
            <Button size="sm" variant="secondary" onClick={onConfigure}>
              <Settings2 className="h-3.5 w-3.5" />
              Configure
            </Button>
          ) : null}
          {actions}
        </div>
      </CardContent>
    </Card>
  );
}
