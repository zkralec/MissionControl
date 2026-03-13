import { cn } from "@/lib/utils";

type JsonViewerProps = {
  value: unknown;
  className?: string;
};

export function JsonViewer({ value, className }: JsonViewerProps): JSX.Element {
  return (
    <pre
      className={cn(
        "max-h-80 overflow-auto rounded-md border border-border bg-muted/50 p-3 text-xs text-foreground",
        className
      )}
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}
