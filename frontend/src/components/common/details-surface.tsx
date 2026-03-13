import type { ReactNode } from "react";
import { X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export function DetailsSurface({
  title,
  open,
  onClose,
  children,
  empty
}: {
  title: string;
  open: boolean;
  onClose: () => void;
  children: ReactNode;
  empty?: ReactNode;
}): JSX.Element {
  return (
    <>
      <div className="hidden lg:block">
        <Card className="sticky top-[124px] h-[calc(100vh-9.25rem)] overflow-hidden border-border/80">
          <CardHeader className="border-b border-border/70 bg-muted/15">
            <div className="flex items-center justify-between gap-2">
              <CardTitle className="text-[12px] uppercase tracking-[0.08em]">{title}</CardTitle>
              {open ? (
                <Button size="sm" variant="ghost" onClick={onClose}>
                  <X className="h-4 w-4" />
                </Button>
              ) : null}
            </div>
          </CardHeader>
          <CardContent className="h-[calc(100%-3.75rem)] overflow-auto bg-card p-4">
            {open ? children : (empty || <div className="text-sm text-muted-foreground">Select an item to inspect details.</div>)}
          </CardContent>
        </Card>
      </div>

      {open ? (
        <div className="fixed inset-0 z-50 bg-black/35 lg:hidden" onClick={onClose}>
          <div className="absolute inset-x-0 bottom-0 max-h-[85vh] rounded-t-xl border border-border bg-card shadow-xl" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between border-b border-border px-4 py-3">
              <div className="text-xs font-semibold uppercase tracking-[0.08em]">{title}</div>
              <Button size="sm" variant="ghost" onClick={onClose}>
                <X className="h-4 w-4" />
              </Button>
            </div>
            <div className="max-h-[calc(85vh-3.2rem)] overflow-auto p-4">{children}</div>
          </div>
        </div>
      ) : null}
    </>
  );
}
