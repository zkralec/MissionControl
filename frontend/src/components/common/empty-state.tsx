import type { ReactNode } from "react";

export function EmptyState({
  title,
  description,
  action
}: {
  title: string;
  description?: string;
  action?: ReactNode;
}): JSX.Element {
  return (
    <div className="rounded-lg border border-dashed border-border/80 bg-muted/25 p-6 text-center">
      <div className="text-sm font-semibold tracking-tight">{title}</div>
      {description ? <p className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-muted-foreground">{description}</p> : null}
      {action ? <div className="mt-3 flex justify-center">{action}</div> : null}
    </div>
  );
}
