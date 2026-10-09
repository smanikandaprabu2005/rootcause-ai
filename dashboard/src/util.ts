export function pct(x: number | undefined | null, digits = 0): string {
  if (x == null || Number.isNaN(x)) return "n/a";
  return `${(x * 100).toFixed(digits)}%`;
}

export function fmtMs(x: number | undefined | null): string {
  if (x == null || Number.isNaN(x)) return "n/a";
  if (x >= 1000) return `${(x / 1000).toFixed(x >= 10000 ? 0 : 1)} s`;
  return `${x >= 100 ? x.toFixed(0) : x.toFixed(1)} ms`;
}

export function fmtNum(x: number | undefined | null): string {
  if (x == null || Number.isNaN(x)) return "n/a";
  return Number(x.toPrecision(4)).toString();
}

export function fmtRel(s: number): string {
  return `${s >= 0 ? "+" : "-"}${Math.abs(s).toFixed(1)}s`;
}

export function words(s: string | null | undefined): string {
  return (s ?? "").replace(/_/g, " ");
}

export function windowSeconds(w?: { start_ns: number; end_ns: number }): number | null {
  if (!w) return null;
  return (w.end_ns - w.start_ns) / 1e9;
}

export function isResolvable(ref?: string | null): boolean {
  return !!ref && (ref.startsWith("log:") || ref.startsWith("trace:"));
}

export function asText(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}
