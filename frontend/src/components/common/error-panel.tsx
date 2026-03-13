import { AlertCircle } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

type ErrorPanelProps = {
  title?: string;
  message: string;
  actionLabel?: string;
  onAction?: () => void;
};

export function ErrorPanel({ title = "Request failed", message, actionLabel = "Retry", onAction }: ErrorPanelProps): JSX.Element {
  return (
    <Card className="border-destructive/40 bg-destructive/10 shadow-[0_1px_2px_rgba(127,29,29,0.1)]">
      <CardContent className="flex flex-wrap items-start justify-between gap-3 p-4 text-sm text-destructive">
        <div className="flex items-start gap-2">
          <AlertCircle className="mt-0.5 h-4 w-4" />
          <div>
            <div className="font-semibold tracking-tight">{title}</div>
            <div className="mt-0.5 text-[13px] leading-relaxed">{message}</div>
          </div>
        </div>
        {onAction ? (
          <Button size="sm" variant="outline" onClick={onAction} className="border-destructive/40 text-destructive hover:bg-destructive/10">
            {actionLabel}
          </Button>
        ) : null}
      </CardContent>
    </Card>
  );
}
