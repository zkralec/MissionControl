import { Link } from "react-router-dom";
import { StatusBadge } from "@/components/common/status-badge";
import { Button } from "@/components/ui/button";
import { formatIso } from "@/lib/utils/format";

export type FeedAction = {
  label: string;
  to: string;
  variant?: "default" | "secondary" | "outline";
};

export type FeedEvent = {
  id?: string;
  title: string;
  explanation?: string;
  message?: string;
  source?: string;
  level?: string;
  createdAt?: string;
  nextAction?: string;
  count?: number;
  actions?: FeedAction[];
};

export function EventFeedList({
  rows,
  emptyText = "No events.",
  loading
}: {
  rows: FeedEvent[];
  emptyText?: string;
  loading?: boolean;
}): JSX.Element {
  if (loading) {
    return <div className="text-sm text-muted-foreground">Loading events…</div>;
  }

  if (rows.length === 0) {
    return <div className="text-sm text-muted-foreground">{emptyText}</div>;
  }

  return (
    <div className="space-y-2">
      {rows.map((row, idx) => (
        <div
          key={row.id || `${row.title}-${row.createdAt || idx}`}
          className="rounded-lg border border-border/80 bg-card p-3"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium tracking-tight break-words">{row.title}</div>
            <div className="flex flex-wrap items-center gap-2">
              {row.count && row.count > 1 ? (
                <span className="rounded-full border border-border bg-muted/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.06em] text-muted-foreground">
                  {row.count}x
                </span>
              ) : null}
              <StatusBadge status={row.level || "info"} />
            </div>
          </div>
          {row.explanation ? <div className="mt-1 text-xs leading-relaxed text-muted-foreground break-words">{row.explanation}</div> : null}
          {!row.explanation && row.message ? <div className="mt-1 text-xs leading-relaxed text-muted-foreground break-words">{row.message}</div> : null}
          {row.nextAction || (row.actions && row.actions.length > 0) ? (
            <div className="mt-2 rounded-md border border-primary/30 bg-primary/10 px-2.5 py-2 text-[11px] text-foreground break-words">
              {row.nextAction ? (
                <div>
                  <span className="font-semibold uppercase tracking-[0.07em] text-primary">Next action</span>
                  <div className="mt-0.5 font-medium text-foreground">{row.nextAction}</div>
                </div>
              ) : null}
              {row.actions && row.actions.length > 0 ? (
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  {row.actions.slice(0, 3).map((action) => (
                    <Button key={`${row.id || row.title}-${action.label}-${action.to}`} asChild size="sm" variant={action.variant || "outline"}>
                      <Link to={action.to}>{action.label}</Link>
                    </Button>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
          <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-muted-foreground">
            {row.source ? <span>source: {row.source}</span> : null}
            {row.createdAt ? <span>{formatIso(row.createdAt)}</span> : null}
          </div>
        </div>
      ))}
    </div>
  );
}
