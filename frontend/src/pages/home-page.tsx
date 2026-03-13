import { useNavigate } from "react-router-dom";
import { AlertTriangle, Bot, Cpu, DollarSign, Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorPanel } from "@/components/common/error-panel";
import { EventFeedList } from "@/components/common/event-feed-list";
import { KpiTile } from "@/components/common/kpi-tile";
import { PageHeader } from "@/components/common/page-header";
import { SectionHeader } from "@/components/common/section-header";
import { StatusBadge } from "@/components/common/status-badge";
import { WorkflowCard } from "@/components/common/workflow-card";
import { useTasks } from "@/features/tasks/queries";
import { useWatchers } from "@/features/watchers/queries";
import { useAiSummary, usePlannerStatus, useStatsToday, useSystemLatest, useTelemetryEvents } from "@/features/telemetry/queries";
import { errorMessage } from "@/lib/utils/errors";
import { formatCost, formatInt, formatPercent } from "@/lib/utils/format";

export function HomePage(): JSX.Element {
  const navigate = useNavigate();
  const statsQuery = useStatsToday();
  const aiQuery = useAiSummary();
  const systemQuery = useSystemLatest();
  const plannerStatusQuery = usePlannerStatus();
  const tasksQuery = useTasks(25);
  const eventsQuery = useTelemetryEvents(30);
  const watchersQuery = useWatchers(20, true);
  const primaryError = [
    statsQuery.error,
    aiQuery.error,
    systemQuery.error,
    plannerStatusQuery.error,
    tasksQuery.error,
    eventsQuery.error,
    watchersQuery.error
  ].find(Boolean);

  const retryAll = (): void => {
    void Promise.all([
      statsQuery.refetch(),
      aiQuery.refetch(),
      systemQuery.refetch(),
      plannerStatusQuery.refetch(),
      tasksQuery.refetch(),
      eventsQuery.refetch(),
      watchersQuery.refetch()
    ]);
  };

  const alertRows = (eventsQuery.data || [])
    .filter((event) => {
      const level = String(event.level || "").toLowerCase();
      const type = String(event.event_type || "").toLowerCase();
      return level === "warning" || level === "error" || type.includes("failed") || type.includes("stale");
    })
    .slice(0, 6)
    .map((row) => ({
      id: row.id,
      title: row.event_type,
      message: row.message,
      source: row.source,
      level: row.level,
      createdAt: row.created_at
    }));

  const activityRows = (eventsQuery.data || []).slice(0, 8).map((row) => ({
    id: row.id,
    title: row.event_type,
    message: row.message,
    source: row.source,
    level: row.level,
    createdAt: row.created_at
  }));

  const enabledWatchers = (watchersQuery.data || []).filter((watcher) => watcher.enabled).slice(0, 3);

  return (
    <div className="space-y-4">
      <PageHeader
        title="Home"
        subtitle="Command-center overview of workflows, runs, alerts, and platform health."
        actions={
          <>
            <Button variant="secondary" onClick={() => navigate("/workflows")}>Manage Workflows</Button>
            <Button onClick={() => navigate("/runs")}>Open Runs</Button>
          </>
        }
      />
      {primaryError ? <ErrorPanel title="Some home data failed to load" message={errorMessage(primaryError)} onAction={retryAll} /> : null}

      <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
        <KpiTile label="Budget Remaining" value={formatCost(statsQuery.data?.remaining_usd)} subtext={`spent ${formatCost(statsQuery.data?.spend_usd)}`} icon={<DollarSign className="h-4 w-4" />} />
        <KpiTile label="Runs Today" value={formatInt(statsQuery.data?.runs_count)} subtext={`${formatInt(statsQuery.data?.success_count)} succeeded`} icon={<Bot className="h-4 w-4" />} />
        <KpiTile label="AI Requests" value={formatInt(aiQuery.data?.requests_total)} subtext={`cost ${formatCost(aiQuery.data?.cost_usd_total)}`} icon={<Zap className="h-4 w-4" />} />
        <KpiTile label="CPU" value={formatPercent(systemQuery.data?.cpu_percent)} subtext={`memory ${formatPercent(systemQuery.data?.memory_percent)}`} icon={<Cpu className="h-4 w-4" />} />
        <KpiTile label="Critical Alerts" value={formatInt(alertRows.length)} tone={alertRows.length > 0 ? "danger" : "success"} icon={<AlertTriangle className="h-4 w-4" />} />
      </section>

      <section>
        <SectionHeader title="Active Watchers" subtitle="Enabled monitored automations with quick configuration access." actions={<Button size="sm" variant="secondary" onClick={() => navigate("/workflows")}>View All</Button>} />
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {watchersQuery.isLoading ? (
            <Card className="md:col-span-2 xl:col-span-3">
              <CardContent className="p-5 text-sm text-muted-foreground">Loading watchers…</CardContent>
            </Card>
          ) : null}
          {enabledWatchers.map((watcher) => (
            <WorkflowCard
              key={watcher.id}
              name={watcher.name}
              taskType={watcher.task_type}
              status={watcher.enabled ? "enabled" : "disabled"}
              cadence={`${watcher.interval_seconds}s`}
              description="Saved watcher automation"
              recentRunMessage={watcher.last_run_summary ? `Last run ${watcher.last_run_summary.task_status}` : "No recent run summary"}
              lastRunStatus={watcher.last_run_summary?.task_status || null}
              onConfigure={() => navigate(`/workflows?watcher=${encodeURIComponent(watcher.id)}`)}
            />
          ))}
          {!watchersQuery.isLoading && enabledWatchers.length === 0 ? (
            <Card className="md:col-span-2 xl:col-span-3">
              <CardContent className="p-5 text-sm text-muted-foreground">No enabled watchers yet. Configure watcher templates in Workflows.</CardContent>
            </Card>
          ) : null}
        </div>
      </section>

      <div className="grid gap-4 lg:grid-cols-2">
        <section>
          <SectionHeader title="Recent Activity" subtitle="Latest event stream from API, worker, and scheduler." />
          <EventFeedList rows={activityRows} emptyText="No recent activity." loading={eventsQuery.isLoading} />
        </section>

        <section>
          <SectionHeader title="High-Signal Alerts" subtitle="Warnings/errors and failure signals requiring operator attention." actions={<Button size="sm" variant="secondary" onClick={() => navigate("/alerts")}>Open Alerts</Button>} />
          <EventFeedList rows={alertRows} emptyText="No active alerts." loading={eventsQuery.isLoading} />
        </section>
      </div>

      <section>
        <SectionHeader title="Run Snapshot" subtitle="Most recent tasks across all task types." actions={<Button size="sm" variant="secondary" onClick={() => navigate("/runs")}>Inspect Runs</Button>} />
        <Card>
          <CardContent className="space-y-2 p-4">
            {tasksQuery.isLoading ? <div className="text-sm text-muted-foreground">Loading runs…</div> : null}
            {!tasksQuery.isLoading && (tasksQuery.data || []).length === 0 ? <div className="text-sm text-muted-foreground">No recent runs available.</div> : null}
            {(tasksQuery.data || []).slice(0, 10).map((task) => (
              <div
                key={task.id}
                className="flex items-center justify-between rounded-md border border-border bg-white/70 px-3 py-2 text-sm"
              >
                <div>
                  <div className="font-medium">{task.task_type}</div>
                  <div className="text-xs text-muted-foreground">{task.id.slice(0, 8)}</div>
                </div>
                <StatusBadge status={task.status} />
              </div>
            ))}
          </CardContent>
        </Card>
      </section>

      <section className="text-xs text-muted-foreground">
        Planner mode: {plannerStatusQuery.data?.mode || "unknown"}, approval: {plannerStatusQuery.data?.require_approval ? (plannerStatusQuery.data?.approved ? "approved" : "awaiting") : "not required"}
      </section>
    </div>
  );
}
