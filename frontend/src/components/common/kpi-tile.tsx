import type { ReactNode } from "react";
import { Card, CardContent } from "@/components/ui/card";

export function KpiTile({
  label,
  value,
  subtext,
  tone = "default",
  icon
}: {
  label: string;
  value: string;
  subtext?: string;
  tone?: "default" | "success" | "warning" | "danger";
  icon?: ReactNode;
}): JSX.Element {
  const toneClass =
    tone === "success"
      ? "text-success"
      : tone === "warning"
        ? "text-warning"
        : tone === "danger"
          ? "text-destructive"
          : "text-foreground";

  return (
    <Card className="overflow-hidden border-border/85">
      <CardContent className="min-h-[110px] p-4">
        <div className="flex items-start justify-between gap-2">
          <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">{label}</div>
          {icon ? <div className="rounded-md border border-border/70 bg-muted/45 p-1 text-muted-foreground">{icon}</div> : null}
        </div>
        <div className={`mt-2.5 text-[1.42rem] font-semibold leading-tight ${toneClass}`}>{value}</div>
        {subtext ? <div className="mt-1 text-[11px] leading-snug text-muted-foreground">{subtext}</div> : null}
      </CardContent>
    </Card>
  );
}
