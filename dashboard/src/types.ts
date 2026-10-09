export type DiagnosisStatus = "supported" | "inconclusive" | "no_incident";

export interface RunnerUp {
  service: string;
  fault_type: string;
  score?: number;
}

export interface Diagnosis {
  status: DiagnosisStatus;
  service: string | null;
  fault_type: string | null;
  confidence: number;
  score?: number;
  summary: string;
  propagation?: string[];
  ruled_out?: string[];
  runner_up?: RunnerUp | null;
}

export interface Anomaly {
  service: string;
  onset_ns?: number;
  anomalous_buckets?: number;
  total_buckets?: number;
  severity?: number;
  deviating?: Record<string, { baseline?: number; worst?: number }>;
}

export interface GraphNode {
  id: string;
  anomalous: boolean;
  starved: boolean;
}

export interface GraphEdge {
  caller: string;
  callee: string;
  calls: number;
  errors?: number;
  error_rate: number;
  p95_ms?: number;
}

export interface Evidence {
  kind: string;
  service: string;
  summary: string;
  ts_ns?: number | null;
  ref?: string | null;
  supports: boolean;
  weight: number;
  decisive?: boolean;
}

export interface Hypothesis {
  service: string;
  fault_type: string;
  score: number;
  confidence: number;
  status: "supported" | "rejected" | "open";
  rejected_because?: string | null;
  evidence: Evidence[];
}

export interface TimelineEntry {
  ts_ns: number;
  t_rel_s: number;
  service: string;
  kind: "log" | "anomaly" | "metric" | string;
  summary: string;
  ref?: string | null;
  on_path: boolean;
}

export interface Verification {
  verified?: boolean;
  reason?: string;
  steps?: string[];
  baseline?: unknown;
  before?: unknown;
  after?: unknown;
}

export interface Proposal {
  service?: string;
  fault_type?: string;
  action?: string;
  description?: string;
  runbook?: unknown;
  [k: string]: unknown;
}

export interface Remediation {
  status: "verified" | "failed" | "unverified" | "no_action" | string;
  reason?: string;
  proposal?: Proposal | null;
  sandbox?: unknown;
  production?: unknown;
  verification?: Verification | null;
}

export interface Report {
  id: string;
  investigation_seconds?: number;
  narrative?: string;
  narrative_source?: "llm" | "evidence" | string;
  window?: { start_ns: number; end_ns: number; baseline_start_ns?: number };
  diagnosis: Diagnosis;
  anomalies: Anomaly[];
  graph: { nodes: GraphNode[]; edges: GraphEdge[] };
  root_candidates: string[];
  propagation_path: string[];
  hypotheses: Hypothesis[];
  timeline: TimelineEntry[];
  remediation?: Remediation | null;
}

export interface StatusInfo {
  counts: { spans: number; logs: number; metrics: number };
  services: string[];
  start_ns?: number;
  end_ns?: number;
  llm_enabled?: boolean;
}

export type EvidenceResult =
  | { ref: string; type: "log"; record: Record<string, unknown> }
  | { ref: string; type: "trace"; spans: Array<Record<string, unknown>> }
  | { ref: string; type: "derived"; note?: string };

export interface SavedInvestigation {
  id: string;
  [k: string]: unknown;
}
