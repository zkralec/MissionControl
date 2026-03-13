export function formatInt(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return Math.round(n).toLocaleString();
}

export function formatCost(value: unknown, digits = 8): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "$0.00000000";
  const safeDigits = Math.max(0, Math.min(8, Math.trunc(digits)));
  return `$${n.toFixed(safeDigits)}`;
}

export function formatPercent(value: unknown): string {
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  return `${n.toFixed(1)}%`;
}

export function formatIso(value: string | null | undefined): string {
  if (!value) return "-";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString();
}

export function formatDurationMs(ms: unknown): string {
  const n = Number(ms);
  if (!Number.isFinite(n) || n < 0) return "-";
  if (n < 1000) return `${Math.round(n)} ms`;
  return `${(n / 1000).toFixed(2)} s`;
}
