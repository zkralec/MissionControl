import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { DataTableWrapper } from "@/components/common/data-table-wrapper";
import { EmptyState } from "@/components/common/empty-state";
import { ErrorPanel } from "@/components/common/error-panel";
import { EventFeedList, type FeedEvent } from "@/components/common/event-feed-list";
import { KpiTile } from "@/components/common/kpi-tile";
import { PageHeader } from "@/components/common/page-header";
import { SectionHeader } from "@/components/common/section-header";
import { StatusBadge } from "@/components/common/status-badge";
import {
  parsePromMetrics,
  useAiSummary,
  useHealthStatus,
  useHeartbeats,
  useHeartbeatSummary,
  usePlannerStatus,
  usePromMetrics,
  useReadyStatus,
  useStatsToday,
  useSystemLatest,
  useSystemRows,
  useTelemetryEvents
} from "@/features/telemetry/queries";
import { errorMessage } from "@/lib/utils/errors";
import { formatCost, formatInt, formatIso, formatPercent } from "@/lib/utils/format";

type ObservabilityView = "overview" | "activity" | "agents";
type AgentRuntimeState = "live" | "stale" | "historical";
type AgentStateRow = {
  agent_name: string;
  status: string;
  last_seen_at: string;
  metadata_json?: unknown;
  is_tracked: boolean;
  runtime_state: AgentRuntimeState;
  last_seen_age_seconds: number | null;
  stale_reason: string;
};

const OBSERVABILITY_VIEWS: Array<{ id: ObservabilityView; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "activity", label: "Activity" },
  { id: "agents", label: "Agents & Planner" }
];

const IMPORTANT_METRIC_KEYS = [
  "mission_control_tasks_db_total",
  "mission_control_runs_db_total",
  "mission_control_artifacts_db_total",
  "mission_control_tasks_created_total",
  "mission_control_tasks_blocked_budget_total",
  "mission_control_auth_rejected_total"
] as const;

const AGENT_STATE_ORDER: Record<AgentRuntimeState, number> = {
  stale: 0,
  live: 1,
  historical: 2
};

function normalizeView(value: string | null): ObservabilityView {
  if (value === "activity") return "activity";
  if (value === "agents") return "agents";
  return "overview";
}

function summarizeText(value: unknown, max = 180): string {
  const text = typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
  if (!text) return "No additional context.";
  if (text.length <= max) return text;
  return `${text.slice(0, max - 1)}…`;
}

function parseIso(value: string | null | undefined): Date | null {
  if (!value) return null;
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function ageSecondsFromIso(value: string | null | undefined, now: Date): number | null {
  const parsed = parseIso(value);
  if (!parsed) return null;
  return Math.max(0, Math.trunc((now.getTime() - parsed.getTime()) / 1000));
}

function formatDurationSeconds(seconds: number): string {
  const safe = Math.max(0, Math.trunc(seconds));
  if (safe < 60) return `${safe}s`;
  if (safe < 3600) {
    const minutes = Math.trunc(safe / 60);
    const remain = safe % 60;
    return remain > 0 ? `${minutes}m ${remain}s` : `${minutes}m`;
  }
  if (safe < 86400) {
    const hours = Math.trunc(safe / 3600);
    const minutes = Math.trunc((safe % 3600) / 60);
    return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`;
  }
  const days = Math.trunc(safe / 86400);
  const hours = Math.trunc((safe % 86400) / 3600);
  return hours > 0 ? `${days}d ${hours}h` : `${days}d`;
}

function formatAge(seconds: number | null): string {
  if (seconds === null) return "unknown";
  return `${formatDurationSeconds(seconds)} ago`;
}

function asNumber(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function toRecord(value: unknown): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function classifyEventLevel(value: string | undefined): "error" | "warning" | "info" {
  const normalized = String(value || "info").toLowerCase();
  if (normalized === "error") return "error";
  if (normalized === "warning") return "warning";
  return "info";
}

function inferDiagnosticAction(eventType: string, message: string): string | undefined {
  const text = `${eventType} ${message}`.toLowerCase();
  if (text.includes("stale") || text.includes("heartbeat") || text.includes("watchdog")) {
    return "Inspect Agents & Planner for stale heartbeats and scheduler health.";
  }
  if (text.includes("planner") || text.includes("approval")) {
    return "Review planner status and recent planner events in Agents & Planner.";
  }
  if (text.includes("failed") || text.includes("blocked_budget")) {
    return "Inspect related task attempts in Runs and check policy thresholds.";
  }
  return undefined;
}

function eventRowFromTelemetry(row: { id?: string; event_type: string; message: string; source: string; level: string; created_at: string }, idx: number): FeedEvent {
  const level = classifyEventLevel(row.level);
  return {
    id: row.id || `${row.event_type}-${row.created_at}-${idx}`,
    title: row.event_type.replace(/_/g, " "),
    explanation: summarizeText(row.message),
    source: row.source,
    level,
    createdAt: row.created_at,
    nextAction: inferDiagnosticAction(row.event_type, row.message)
  };
}

export function ObservabilityPage(): JSX.Element {
  const [searchParams, setSearchParams] = useSearchParams();
  const activeView = normalizeView(searchParams.get("section"));

  const statsQuery = useStatsToday();
  const aiQuery = useAiSummary();
  const healthQuery = useHealthStatus();
  const readyQuery = useReadyStatus();
  const systemLatestQuery = useSystemLatest();
  const systemRowsQuery = useSystemRows(80);
  const plannerQuery = usePlannerStatus();
  const eventsQuery = useTelemetryEvents(220);
  const heartbeatsQuery = useHeartbeats(80);
  const heartbeatSummaryQuery = useHeartbeatSummary(undefined, 200);
  const promQuery = usePromMetrics();

  const promAll = useMemo(() => parsePromMetrics(promQuery.data || ""), [promQuery.data]);
  const promPairs = useMemo(
    () =>
      Object.entries(promAll)
        .slice(0, 40)
        .map(([metric, value]) => ({ metric, value })),
    [promAll]
  );

  const importantMetrics = useMemo(() => {
    return IMPORTANT_METRIC_KEYS
      .map((key) => ({ key, value: promAll[key] }))
      .filter((row) => Number.isFinite(row.value));
  }, [promAll]);

  const eventRows = useMemo(
    () => (eventsQuery.data || []).map((row, idx) => eventRowFromTelemetry(row, idx)),
    [eventsQuery.data]
  );

  const highSignalRows = useMemo(
    () => eventRows.filter((row) => row.level === "error" || row.level === "warning").slice(0, 16),
    [eventRows]
  );

  const infoRows = useMemo(() => eventRows.filter((row) => row.level === "info").slice(0, 24), [eventRows]);

  const eventLevelCounts = useMemo(() => {
    return (eventsQuery.data || []).reduce(
      (acc, row) => {
        const level = classifyEventLevel(row.level);
        acc[level] += 1;
        return acc;
      },
      { error: 0, warning: 0, info: 0 }
    );
  }, [eventsQuery.data]);

  const topSources = useMemo(() => {
    const counts = new Map<string, number>();
    (eventsQuery.data || []).forEach((row) => {
      const key = String(row.source || "unknown");
      counts.set(key, (counts.get(key) || 0) + 1);
    });
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .slice(0, 4)
      .map(([source, count]) => ({ source, count }));
  }, [eventsQuery.data]);

  const plannerRecentRows = useMemo(() => {
    const events = Array.isArray(plannerQuery.data?.recent_events) ? plannerQuery.data.recent_events : [];
    return events.slice(0, 12).map((row, idx) => {
      const rec = toRecord(row);
      const eventType = String(rec.event_type || `planner_event_${idx + 1}`);
      const message = String(rec.message || "Planner event recorded.");
      const createdAt = String(rec.created_at || "");
      const source = String(rec.source || "planner");
      const level = classifyEventLevel(String(rec.level || "info"));
      return {
        id: String(rec.id || `${eventType}-${createdAt}-${idx}`),
        title: eventType.replace(/_/g, " "),
        explanation: summarizeText(message),
        source,
        level,
        createdAt,
        nextAction: inferDiagnosticAction(eventType, message)
      } satisfies FeedEvent;
    });
  }, [plannerQuery.data?.recent_events]);

  const staleAfterSeconds = Math.max(1, Math.trunc(heartbeatSummaryQuery.data?.stale_after_seconds || 0));

  const trackedAgentNames = useMemo(
    () =>
      (heartbeatSummaryQuery.data?.tracked_agent_names || [])
        .map((value) => String(value || "").trim())
        .filter(Boolean),
    [heartbeatSummaryQuery.data?.tracked_agent_names]
  );

  const trackedAgentLabel = trackedAgentNames.length > 0 ? trackedAgentNames.join(", ") : "none";
  const staleThresholdLabel = `${formatDurationSeconds(staleAfterSeconds)} (${formatInt(staleAfterSeconds)}s)`;
  const staleCutoffLabel = heartbeatSummaryQuery.data?.stale_cutoff_at
    ? formatIso(heartbeatSummaryQuery.data.stale_cutoff_at)
    : "-";

  const agentStateRows = useMemo<AgentStateRow[]>(() => {
    const summary = heartbeatSummaryQuery.data;
    const trackedSet = new Set(
      (summary?.tracked_agent_names || [])
        .map((value) => String(value || "").trim())
        .filter(Boolean)
    );
    const staleSet = new Set(
      (summary?.stale_current_rows || [])
        .map((row) => String(row.agent_name || "").trim())
        .filter(Boolean)
    );
    const historicalSet = new Set(
      (summary?.historical_dead_rows || [])
        .map((row) => String(row.agent_name || "").trim())
        .filter(Boolean)
    );
    const byAgent = new Map<string, { agent_name: string; status: string; last_seen_at: string; metadata_json?: unknown }>();
    (heartbeatsQuery.data || []).forEach((row) => {
      const agentName = String(row.agent_name || "").trim();
      if (!agentName) return;
      byAgent.set(agentName, row);
    });
    (summary?.stale_current_rows || []).forEach((row) => {
      const agentName = String(row.agent_name || "").trim();
      if (!agentName || byAgent.has(agentName)) return;
      byAgent.set(agentName, row);
    });

    const now = parseIso(summary?.captured_at) || new Date();
    const thresholdText = formatDurationSeconds(staleAfterSeconds);

    return Array.from(byAgent.values())
      .map((row) => {
        const agentName = String(row.agent_name || "").trim();
        const status = String(row.status || "unknown");
        const statusLower = status.toLowerCase();
        const isTracked = trackedSet.has(agentName);
        const ageSeconds = ageSecondsFromIso(row.last_seen_at, now);
        const exceedsThreshold = ageSeconds === null ? true : ageSeconds > staleAfterSeconds;

        let runtimeState: AgentRuntimeState;
        if (isTracked) {
          runtimeState = staleSet.has(agentName) || statusLower === "stale" || statusLower === "missing" || exceedsThreshold ? "stale" : "live";
        } else {
          runtimeState = historicalSet.has(agentName) || statusLower === "stale" || exceedsThreshold ? "historical" : "live";
        }

        const metadata = toRecord(row.metadata_json);
        const watchdogStaleFor = Number(metadata.watchdog_stale_for_seconds);
        let staleReason: string;
        if (runtimeState === "stale") {
          if (!row.last_seen_at || statusLower === "missing") {
            staleReason = `No heartbeat observed for tracked agent in the current window (threshold ${thresholdText}).`;
          } else if (Number.isFinite(watchdogStaleFor) && watchdogStaleFor > 0) {
            staleReason = `Watchdog marked this agent stale after ${formatDurationSeconds(watchdogStaleFor)} without heartbeat (threshold ${thresholdText}).`;
          } else {
            staleReason = `Last heartbeat ${formatAge(ageSeconds)}; exceeds stale threshold (${thresholdText}).`;
          }
        } else if (runtimeState === "historical") {
          staleReason = `Untracked historical/offline identity; excluded from current stale-agent incidents.`;
        } else if (isTracked) {
          staleReason = `Tracked agent heartbeat is within stale threshold (${thresholdText}).`;
        } else {
          staleReason = "Untracked agent currently reporting heartbeats.";
        }

        return {
          agent_name: agentName,
          status,
          last_seen_at: String(row.last_seen_at || ""),
          metadata_json: row.metadata_json,
          is_tracked: isTracked,
          runtime_state: runtimeState,
          last_seen_age_seconds: ageSeconds,
          stale_reason: staleReason
        } satisfies AgentStateRow;
      })
      .sort((a, b) => {
        const stateDelta = AGENT_STATE_ORDER[a.runtime_state] - AGENT_STATE_ORDER[b.runtime_state];
        if (stateDelta !== 0) return stateDelta;
        const aAge = a.last_seen_age_seconds ?? Number.MAX_SAFE_INTEGER;
        const bAge = b.last_seen_age_seconds ?? Number.MAX_SAFE_INTEGER;
        if (a.runtime_state === "live") return aAge - bAge;
        return bAge - aAge;
      });
  }, [heartbeatSummaryQuery.data, heartbeatsQuery.data, staleAfterSeconds]);

  const liveAgentRows = useMemo(
    () =>
      agentStateRows
        .filter((row) => row.runtime_state === "live")
        .slice(0, 12)
        .map((row) => ({
          id: `live-${row.agent_name}-${row.last_seen_at}`,
          title: row.is_tracked ? `Live agent: ${row.agent_name}` : `Live untracked agent: ${row.agent_name}`,
          explanation: `${row.stale_reason} Last seen ${formatAge(row.last_seen_age_seconds)}.`,
          source: "heartbeat",
          level: "info",
          createdAt: row.last_seen_at || heartbeatSummaryQuery.data?.captured_at || ""
        })),
    [agentStateRows, heartbeatSummaryQuery.data?.captured_at]
  );

  const staleAgentRows = useMemo(
    () =>
      agentStateRows
        .filter((row) => row.runtime_state === "stale")
        .slice(0, 12)
        .map((row) => ({
          id: `${row.agent_name}-${row.last_seen_at}`,
          title: `Stale agent: ${row.agent_name}`,
          explanation: row.stale_reason,
          source: "heartbeat",
          level: "warning",
          createdAt: row.last_seen_at || heartbeatSummaryQuery.data?.captured_at || "",
          nextAction: "Check worker/scheduler process health and logs."
        })),
    [agentStateRows, heartbeatSummaryQuery.data?.captured_at]
  );

  const historicalDeadRows = useMemo(
    () =>
      agentStateRows
        .filter((row) => row.runtime_state === "historical")
        .slice(0, 12)
        .map((row) => ({
          id: `historical-${row.agent_name}-${row.last_seen_at}`,
          title: `Historical/offline agent: ${row.agent_name}`,
          explanation: `${row.stale_reason} Last seen ${formatAge(row.last_seen_age_seconds)}.`,
          source: "heartbeat",
          level: "info",
          createdAt: row.last_seen_at || heartbeatSummaryQuery.data?.captured_at || "",
          nextAction: "Historical identity kept for diagnostics; no immediate action unless this name should still be tracked."
        })),
    [agentStateRows, heartbeatSummaryQuery.data?.captured_at]
  );

  const aiFailureRate = useMemo(() => {
    const total = asNumber(aiQuery.data?.requests_total);
    const failed = asNumber(aiQuery.data?.failed_total);
    if (total <= 0) return 0;
    return (failed / total) * 100;
  }, [aiQuery.data]);

  const latestSystem = systemLatestQuery.data;

  const diagnosticWarnings = useMemo(() => {
    const warnings: Array<{ id: string; title: string; message: string; level: "warning" | "error" }> = [];

    if (String(readyQuery.data?.status || "not_ready").toLowerCase() !== "ready") {
      warnings.push({
        id: "ready",
        title: "Readiness degraded",
        message: readyQuery.data?.error ? `Ready endpoint returned not_ready: ${readyQuery.data.error}` : "Ready endpoint is not in ready state.",
        level: "error"
      });
    }

    if (asNumber(latestSystem?.cpu_percent) >= 90) {
      warnings.push({
        id: "cpu",
        title: "High CPU",
        message: `CPU is ${formatPercent(latestSystem?.cpu_percent)} on latest sample.`,
        level: "warning"
      });
    }

    if (asNumber(latestSystem?.memory_percent) >= 90) {
      warnings.push({
        id: "memory",
        title: "High memory",
        message: `Memory is ${formatPercent(latestSystem?.memory_percent)} on latest sample.`,
        level: "warning"
      });
    }

    if ((heartbeatSummaryQuery.data?.stale_current_agents || 0) > 0) {
      warnings.push({
        id: "stale-agents",
        title: "Stale agents detected",
        message: `${formatInt(heartbeatSummaryQuery.data?.stale_current_agents || 0)} tracked agents are stale in the current heartbeat window.`,
        level: "error"
      });
    }

    if (aiFailureRate >= 20) {
      warnings.push({
        id: "ai-failure-rate",
        title: "Elevated AI failure rate",
        message: `AI request failure rate is ${aiFailureRate.toFixed(1)}%.`,
        level: "warning"
      });
    }

    return warnings;
  }, [readyQuery.data, latestSystem, heartbeatSummaryQuery.data, aiFailureRate]);

  const pageError = [
    statsQuery.error,
    aiQuery.error,
    healthQuery.error,
    readyQuery.error,
    systemLatestQuery.error,
    systemRowsQuery.error,
    plannerQuery.error,
    eventsQuery.error,
    heartbeatsQuery.error,
    heartbeatSummaryQuery.error,
    promQuery.error
  ].find(Boolean);

  const retryAll = (): void => {
    void Promise.all([
      statsQuery.refetch(),
      aiQuery.refetch(),
      healthQuery.refetch(),
      readyQuery.refetch(),
      systemLatestQuery.refetch(),
      systemRowsQuery.refetch(),
      plannerQuery.refetch(),
      eventsQuery.refetch(),
      heartbeatsQuery.refetch(),
      heartbeatSummaryQuery.refetch(),
      promQuery.refetch()
    ]);
  };

  const selectView = (view: ObservabilityView): void => {
    const next = new URLSearchParams(searchParams);
    if (view === "overview") {
      next.delete("section");
    } else {
      next.set("section", view);
    }
    setSearchParams(next, { replace: true });
  };

  return (
    <div className="space-y-4">
      <PageHeader
        title="Observability"
        subtitle="Read-only diagnostic workspace for telemetry, activity streams, and runtime health." 
        actions={<Button variant="secondary" onClick={retryAll}>Refresh</Button>}
      />
      {pageError ? <ErrorPanel title="Observability data failed to load" message={errorMessage(pageError)} onAction={retryAll} /> : null}

      <Card>
        <CardContent className="space-y-3 p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs text-muted-foreground">
              <span className="font-semibold uppercase tracking-[0.08em]">Read-only diagnostics</span>
              {" · "}
              No mutation controls are available in this workspace.
            </div>
            <StatusBadge status="info" />
          </div>
          <div className="flex flex-wrap gap-2">
            {OBSERVABILITY_VIEWS.map((view) => (
              <Button
                key={view.id}
                size="sm"
                variant={activeView === view.id ? "default" : "secondary"}
                onClick={() => selectView(view.id)}
              >
                {view.label}
              </Button>
            ))}
          </div>
        </CardContent>
      </Card>

      {activeView === "overview" ? (
        <div className="space-y-4">
          <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <KpiTile label="API Health" value={String(healthQuery.data?.status || "unknown")} tone={String(healthQuery.data?.status || "").toLowerCase() === "ok" ? "success" : "warning"} />
            <KpiTile label="Readiness" value={String(readyQuery.data?.status || "unknown")} tone={String(readyQuery.data?.status || "").toLowerCase() === "ready" ? "success" : "danger"} />
            <KpiTile label="Spend Today" value={formatCost(statsQuery.data?.spend_usd)} subtext={`remaining ${formatCost(statsQuery.data?.remaining_usd)}`} />
            <KpiTile label="AI Fail Rate" value={`${aiFailureRate.toFixed(1)}%`} tone={aiFailureRate >= 20 ? "danger" : aiFailureRate >= 10 ? "warning" : "success"} subtext={`${formatInt(aiQuery.data?.failed_total)} failed / ${formatInt(aiQuery.data?.requests_total)} total`} />
            <KpiTile label="CPU" value={formatPercent(latestSystem?.cpu_percent)} subtext={`memory ${formatPercent(latestSystem?.memory_percent)}`} tone={asNumber(latestSystem?.cpu_percent) >= 90 ? "danger" : "default"} />
            <KpiTile
              label="Stale Agents"
              value={formatInt(heartbeatSummaryQuery.data?.stale_current_agents || 0)}
              tone={(heartbeatSummaryQuery.data?.stale_current_agents || 0) > 0 ? "danger" : "success"}
              subtext={`${formatInt(heartbeatSummaryQuery.data?.historical_dead_agents || 0)} historical`}
            />
          </section>

          <section>
            <SectionHeader title="Diagnostic Warnings" subtitle="Elevated signals derived from readiness, metrics, and heartbeat telemetry." />
            {diagnosticWarnings.length === 0 ? (
              <EmptyState title="No active diagnostic warnings" description="Current telemetry snapshot does not indicate elevated system risk." />
            ) : (
              <div className="space-y-2">
                {diagnosticWarnings.map((warning) => (
                  <ErrorPanel key={warning.id} title={warning.title} message={warning.message} />
                ))}
              </div>
            )}
          </section>

          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <CardContent className="p-4">
                <SectionHeader title="System Snapshot" subtitle="Latest resource sample and readiness posture." />
                <div className="grid gap-2 sm:grid-cols-2">
                  <div className="rounded border border-border bg-muted/20 p-2 text-xs">
                    <div className="font-medium uppercase tracking-[0.06em] text-muted-foreground">CPU</div>
                    <div className="mt-1">{formatPercent(latestSystem?.cpu_percent)}</div>
                  </div>
                  <div className="rounded border border-border bg-muted/20 p-2 text-xs">
                    <div className="font-medium uppercase tracking-[0.06em] text-muted-foreground">Memory</div>
                    <div className="mt-1">{formatPercent(latestSystem?.memory_percent)}</div>
                  </div>
                  <div className="rounded border border-border bg-muted/20 p-2 text-xs">
                    <div className="font-medium uppercase tracking-[0.06em] text-muted-foreground">Disk</div>
                    <div className="mt-1">{formatPercent(latestSystem?.disk_percent)}</div>
                  </div>
                  <div className="rounded border border-border bg-muted/20 p-2 text-xs">
                    <div className="font-medium uppercase tracking-[0.06em] text-muted-foreground">Planner Mode</div>
                    <div className="mt-1">{plannerQuery.data?.mode || "unknown"}</div>
                  </div>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardContent className="p-4">
                <SectionHeader title="Core Counters" subtitle="Key mission-control counters from Prometheus snapshot." />
                {importantMetrics.length === 0 ? (
                  <EmptyState title="No core counters available" description="Prometheus snapshot did not include expected mission-control metrics." />
                ) : (
                  <div className="grid gap-2 sm:grid-cols-2">
                    {importantMetrics.map((metric) => (
                      <div key={metric.key} className="rounded border border-border bg-muted/20 p-2 text-xs">
                        <div className="font-mono text-[10px] text-muted-foreground">{metric.key}</div>
                        <div className="mt-1 text-sm font-semibold">{formatInt(metric.value)}</div>
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </div>

          <details className="rounded-lg border border-border/80 bg-card p-4">
            <summary className="cursor-pointer text-sm font-semibold tracking-tight">Detailed system and metrics tables</summary>
            <div className="mt-4 space-y-4">
              <DataTableWrapper
                title="System Metrics"
                subtitle="Recent CPU/memory/disk samples."
                loading={systemRowsQuery.isLoading}
                error={systemRowsQuery.error ? errorMessage(systemRowsQuery.error) : null}
                onRetry={() => void systemRowsQuery.refetch()}
                isEmpty={!systemRowsQuery.isLoading && (systemRowsQuery.data || []).length === 0}
              >
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Captured</TableHead>
                      <TableHead>CPU</TableHead>
                      <TableHead>Memory</TableHead>
                      <TableHead>Disk</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {(systemRowsQuery.data || []).map((row) => (
                      <TableRow key={row.id}>
                        <TableCell>{row.created_at}</TableCell>
                        <TableCell>{formatPercent(row.cpu_percent)}</TableCell>
                        <TableCell>{formatPercent(row.memory_percent)}</TableCell>
                        <TableCell>{formatPercent(row.disk_percent)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </DataTableWrapper>

              <DataTableWrapper
                title="Prometheus Metrics"
                subtitle="Parsed raw `/metrics` lines."
                loading={promQuery.isLoading}
                error={promQuery.error ? errorMessage(promQuery.error) : null}
                onRetry={() => void promQuery.refetch()}
                isEmpty={!promQuery.isLoading && promPairs.length === 0}
              >
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Metric</TableHead>
                      <TableHead>Value</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {promPairs.map((row) => (
                      <TableRow key={row.metric}>
                        <TableCell className="font-mono text-xs">{row.metric}</TableCell>
                        <TableCell>{row.value}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </DataTableWrapper>
            </div>
          </details>
        </div>
      ) : null}

      {activeView === "activity" ? (
        <div className="space-y-4">
          <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <KpiTile label="Events (window)" value={formatInt((eventsQuery.data || []).length)} />
            <KpiTile label="Errors" value={formatInt(eventLevelCounts.error)} tone={eventLevelCounts.error > 0 ? "danger" : "success"} />
            <KpiTile label="Warnings" value={formatInt(eventLevelCounts.warning)} tone={eventLevelCounts.warning > 0 ? "warning" : "success"} />
            <KpiTile label="Sources" value={formatInt(topSources.length)} subtext={topSources[0] ? `top ${topSources[0].source}` : "no source data"} />
          </section>

          <Card>
            <CardContent className="p-4">
              <SectionHeader title="High-Signal Activity" subtitle="Recent warning/error diagnostic events." />
              {highSignalRows.length === 0 && !eventsQuery.isLoading ? (
                <EmptyState title="No high-signal activity" description="No warning/error events in the current telemetry window." />
              ) : (
                <EventFeedList rows={highSignalRows} emptyText="No warning/error events." loading={eventsQuery.isLoading} />
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent className="p-4">
              <SectionHeader title="Informational Recent" subtitle="Lower-severity context events for timeline correlation." />
              {infoRows.length === 0 && !eventsQuery.isLoading ? (
                <EmptyState title="No informational activity" description="No recent low-severity events were captured." />
              ) : (
                <EventFeedList rows={infoRows} emptyText="No informational events." loading={eventsQuery.isLoading} />
              )}
            </CardContent>
          </Card>
        </div>
      ) : null}

      {activeView === "agents" ? (
        <div className="space-y-4">
          <section className="grid gap-3 sm:grid-cols-2 xl:grid-cols-6">
            <KpiTile label="Tracked Agents" value={formatInt(heartbeatSummaryQuery.data?.tracked_agents_total || 0)} />
            <KpiTile
              label="Active Tracked"
              value={formatInt(heartbeatSummaryQuery.data?.active_tracked_agents || 0)}
              tone={(heartbeatSummaryQuery.data?.active_tracked_agents || 0) > 0 ? "success" : "warning"}
            />
            <KpiTile
              label="Stale Current"
              value={formatInt(heartbeatSummaryQuery.data?.stale_current_agents || 0)}
              tone={(heartbeatSummaryQuery.data?.stale_current_agents || 0) > 0 ? "danger" : "success"}
            />
            <KpiTile
              label="Historical Dead"
              value={formatInt(heartbeatSummaryQuery.data?.historical_dead_agents || 0)}
              subtext="excluded from current stale"
            />
            <KpiTile label="Planner Mode" value={plannerQuery.data?.mode || "unknown"} subtext={`interval ${plannerQuery.data?.interval_sec || "-"}s`} />
            <KpiTile label="Planner Enabled" value={plannerQuery.data?.enabled ? "true" : "false"} tone={plannerQuery.data?.enabled ? "success" : "warning"} />
          </section>

          <Card>
            <CardContent className="p-4">
              <SectionHeader title="Heartbeat Classification Rules" subtitle="How live, stale, and historical/offline states are derived in this read-only view." />
              <div className="space-y-2 text-xs text-muted-foreground">
                <div>
                  <span className="font-semibold text-foreground">Stale threshold:</span> {staleThresholdLabel} (cutoff {staleCutoffLabel})
                </div>
                <div>
                  <span className="font-semibold text-foreground">Tracked agents:</span> {trackedAgentLabel}
                </div>
                <div>
                  A tracked agent is <span className="font-semibold text-foreground">stale</span> when heartbeat age exceeds the threshold or no heartbeat exists.
                  Untracked stale identities are labeled <span className="font-semibold text-foreground">historical/offline</span> and excluded from current stale incidents.
                </div>
              </div>
            </CardContent>
          </Card>

          <div className="grid gap-4 xl:grid-cols-3">
            <Card>
              <CardContent className="p-4">
                <SectionHeader title="Live Agent Signals" subtitle="Agents currently within the heartbeat freshness threshold." />
                {liveAgentRows.length === 0 && !heartbeatSummaryQuery.isLoading ? (
                  <EmptyState title="No live agents in snapshot" description="No agents are currently within the configured heartbeat freshness window." />
                ) : (
                  <EventFeedList rows={liveAgentRows} emptyText="No live agents." loading={heartbeatSummaryQuery.isLoading} />
                )}
              </CardContent>
            </Card>

            <Card>
              <CardContent className="p-4">
                <SectionHeader title="Stale Agent Signals" subtitle="Tracked runtime agents that currently require diagnostics." />
                {staleAgentRows.length === 0 && !heartbeatSummaryQuery.isLoading ? (
                  <EmptyState title="No stale agents" description="All observed agents are reporting heartbeats in the configured freshness window." />
                ) : (
                  <EventFeedList rows={staleAgentRows} emptyText="No stale agents." loading={heartbeatSummaryQuery.isLoading} />
                )}
              </CardContent>
            </Card>

            <Card>
              <CardContent className="p-4">
                <SectionHeader title="Planner Recent Activity" subtitle="Latest planner telemetry events and context." />
                {plannerRecentRows.length === 0 && !plannerQuery.isLoading ? (
                  <EmptyState title="No planner events" description="Planner telemetry stream has no recent activity entries." />
                ) : (
                  <EventFeedList rows={plannerRecentRows} emptyText="No planner events." loading={plannerQuery.isLoading} />
                )}
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardContent className="p-4">
              <SectionHeader title="Historical/Offline Agents" subtitle="Older non-tracked identities retained for forensics, not current runtime incidents." />
              {historicalDeadRows.length === 0 && !heartbeatSummaryQuery.isLoading ? (
                <EmptyState title="No historical dead agents" description="No retired or historical agent identities are in the stale window." />
              ) : (
                <EventFeedList rows={historicalDeadRows} emptyText="No historical dead agents." loading={heartbeatSummaryQuery.isLoading} />
              )}
            </CardContent>
          </Card>

          <DataTableWrapper
            title="Agent Heartbeats"
            subtitle="Recent heartbeat snapshots with runtime-state classification and stale rationale."
            loading={heartbeatsQuery.isLoading}
            error={heartbeatsQuery.error ? errorMessage(heartbeatsQuery.error) : null}
            onRetry={() => void heartbeatsQuery.refetch()}
            isEmpty={!heartbeatsQuery.isLoading && agentStateRows.length === 0}
          >
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Agent</TableHead>
                  <TableHead>Runtime State</TableHead>
                  <TableHead>Heartbeat Status</TableHead>
                  <TableHead>Last Seen</TableHead>
                  <TableHead>Age</TableHead>
                  <TableHead>Stale Context</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {agentStateRows.map((row) => (
                  <TableRow key={`${row.agent_name}-${row.last_seen_at}-${row.runtime_state}`}>
                    <TableCell>
                      <div className="space-y-1">
                        <div>{row.agent_name}</div>
                        <div className="text-[11px] text-muted-foreground">{row.is_tracked ? "tracked runtime agent" : "untracked identity"}</div>
                      </div>
                    </TableCell>
                    <TableCell><StatusBadge status={row.runtime_state} /></TableCell>
                    <TableCell><StatusBadge status={row.status} /></TableCell>
                    <TableCell>{row.last_seen_at ? formatIso(row.last_seen_at) : "-"}</TableCell>
                    <TableCell>{formatAge(row.last_seen_age_seconds)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{row.stale_reason}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </DataTableWrapper>
        </div>
      ) : null}
    </div>
  );
}
