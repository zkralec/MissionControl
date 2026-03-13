import type { ReactNode } from "react";

export function PageHeader({
  title,
  subtitle,
  actions
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <div className="rounded-xl border border-border/80 bg-card/95 px-4 py-3.5 shadow-[0_1px_2px_rgba(16,24,40,0.06)]">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-[1.36rem] font-semibold tracking-tight text-foreground md:text-[1.42rem]">{title}</h1>
          {subtitle ? <p className="max-w-4xl text-[13px] leading-relaxed text-muted-foreground">{subtitle}</p> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2 self-start">{actions}</div> : null}
      </div>
    </div>
  );
}
