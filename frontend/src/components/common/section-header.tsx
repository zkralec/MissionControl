import type { ReactNode } from "react";

export function SectionHeader({
  title,
  subtitle,
  actions
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <div className="mb-3 flex flex-wrap items-end justify-between gap-2 border-b border-border/75 pb-2.5">
      <div className="space-y-1">
        <h2 className="text-[13px] font-semibold uppercase tracking-[0.08em] text-foreground">{title}</h2>
        {subtitle ? <p className="text-[12px] leading-relaxed text-muted-foreground">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}
