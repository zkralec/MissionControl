import type { ReactNode } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState } from "@/components/common/empty-state";

export function DataTableWrapper({
  title,
  subtitle,
  actions,
  loading,
  error,
  onRetry,
  retryLabel,
  isEmpty,
  emptyTitle,
  emptyDescription,
  children
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  retryLabel?: string;
  isEmpty?: boolean;
  emptyTitle?: string;
  emptyDescription?: string;
  children: ReactNode;
}): JSX.Element {
  return (
    <Card className="border-border/80">
      <CardHeader className="border-b border-border/70 bg-muted/15">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div>
            <CardTitle className="text-[13px] uppercase tracking-[0.08em]">{title}</CardTitle>
            {subtitle ? <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{subtitle}</p> : null}
          </div>
          {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
        </div>
      </CardHeader>
      <CardContent className="p-3.5">
        {loading ? <div className="py-10 text-center text-sm text-muted-foreground">Loading…</div> : null}
        {!loading && error ? (
          <div className="space-y-2 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
            <div>{error}</div>
            {onRetry ? (
              <button
                type="button"
                className="rounded-md border border-destructive/40 px-2 py-1 text-xs font-medium hover:bg-destructive/10"
                onClick={onRetry}
              >
                {retryLabel || "Retry"}
              </button>
            ) : null}
          </div>
        ) : null}
        {!loading && !error && isEmpty ? (
          <EmptyState title={emptyTitle || "No data"} description={emptyDescription || "No rows to display."} />
        ) : null}
        {!loading && !error && !isEmpty ? children : null}
      </CardContent>
    </Card>
  );
}
