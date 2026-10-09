import type {
  EvidenceResult,
  Report,
  SavedInvestigation,
  StatusInfo,
} from "./types";

async function http<T>(url: string, init?: RequestInit, timeoutMs = 0): Promise<T> {
  const ctrl = new AbortController();
  const timer = timeoutMs ? setTimeout(() => ctrl.abort(), timeoutMs) : null;
  try {
    const res = await fetch(url, { ...init, signal: ctrl.signal });
    if (!res.ok) {
      let detail = "";
      try {
        const body = await res.json();
        detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body?.detail ?? body);
      } catch {
        /* non-JSON error body */
      }
      throw new Error(`HTTP ${res.status}${detail ? `: ${detail}` : ""}`);
    }
    return (await res.json()) as T;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/** Fill in optional collections so the UI never dereferences undefined. */
export function normalizeReport(raw: Partial<Report> & { diagnosis: Report["diagnosis"] }): Report {
  return {
    ...raw,
    id: String(raw.id ?? "unknown"),
    anomalies: raw.anomalies ?? [],
    graph: {
      nodes: raw.graph?.nodes ?? [],
      edges: raw.graph?.edges ?? [],
    },
    root_candidates: raw.root_candidates ?? [],
    propagation_path: raw.propagation_path ?? raw.diagnosis?.propagation ?? [],
    hypotheses: (raw.hypotheses ?? []).map((h) => ({ ...h, evidence: h.evidence ?? [] })),
    timeline: raw.timeline ?? [],
  };
}

export const getStatus = () => http<StatusInfo>("/api/status", undefined, 4000);

export async function investigate(): Promise<Report> {
  const r = await http<Report>("/api/investigate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ use_llm: false }),
  });
  return normalizeReport(r);
}

export async function loadSample(): Promise<Report> {
  const r = await http<Report>("./sample-report.json");
  return normalizeReport(r);
}

export async function listInvestigations(): Promise<SavedInvestigation[]> {
  const r = await http<unknown>("/api/investigations", undefined, 4000);
  const arr = Array.isArray(r)
    ? r
    : Array.isArray((r as { investigations?: unknown })?.investigations)
      ? ((r as { investigations: unknown[] }).investigations)
      : [];
  return arr
    .map((x) => (typeof x === "string" ? { id: x } : (x as SavedInvestigation)))
    .filter((x) => x && x.id != null)
    .map((x) => ({ ...x, id: String(x.id) }));
}

export async function getInvestigation(id: string): Promise<Report> {
  return normalizeReport(await http<Report>(`/api/investigations/${encodeURIComponent(id)}`));
}

export const getEvidence = (ref: string) =>
  http<EvidenceResult>(`/api/evidence?ref=${encodeURIComponent(ref)}`, undefined, 10000);
