import { useEffect, useMemo, useRef, useState } from "react";
import { getEvidence } from "./api";
import type { EvidenceResult } from "./types";
import { IconX } from "./icons";
import { asText, fmtMs, isResolvable } from "./util";

type State =
  | { s: "idle" }
  | { s: "loading" }
  | { s: "demo" }
  | { s: "error"; msg: string }
  | { s: "ok"; data: EvidenceResult };

const pick = (o: Record<string, unknown>, keys: string[]): unknown => {
  for (const k of keys) if (o[k] != null) return o[k];
  return undefined;
};

interface SpanNode {
  raw: Record<string, unknown>;
  id: string;
  children: SpanNode[];
  start: number;
}

function buildTree(spans: Array<Record<string, unknown>>): SpanNode[] {
  const nodes: SpanNode[] = spans.map((raw, i) => ({
    raw,
    id: String(pick(raw, ["span_id", "id"]) ?? `#${i}`),
    children: [],
    start: Number(pick(raw, ["start_ns", "start_time_ns", "start_time", "ts_ns"]) ?? i),
  }));
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const roots: SpanNode[] = [];
  for (const n of nodes) {
    const pid = pick(n.raw, ["parent_span_id", "parent_id", "parent"]);
    const parent = pid != null && pid !== "" ? byId.get(String(pid)) : undefined;
    if (parent && parent !== n) parent.children.push(n);
    else roots.push(n);
  }
  const sort = (a: SpanNode[]) => {
    a.sort((x, y) => x.start - y.start);
    a.forEach((c) => sort(c.children));
  };
  sort(roots);
  return roots;
}

function spanMs(raw: Record<string, unknown>): number | null {
  const d = pick(raw, ["duration_ms"]);
  if (typeof d === "number") return d;
  const dn = pick(raw, ["duration_ns"]);
  if (typeof dn === "number") return dn / 1e6;
  const s = pick(raw, ["start_ns", "start_time_ns"]);
  const e = pick(raw, ["end_ns", "end_time_ns"]);
  if (typeof s === "number" && typeof e === "number") return (e - s) / 1e6;
  return null;
}

function SpanRow({ n, depth, maxMs, focusId }: { n: SpanNode; depth: number; maxMs: number; focusId: string | null }) {
  const r = n.raw;
  const ms = spanMs(r);
  const name = asText(pick(r, ["name", "operation", "span_name"])) || n.id;
  const svc = asText(pick(r, ["service", "service_name"]));
  const status = asText(pick(r, ["status", "status_code"]));
  const bad = /error|fail|timeout/i.test(status) || pick(r, ["error"]) === true;
  const used = new Set(["span_id", "id", "parent_span_id", "parent_id", "parent", "name", "operation", "span_name", "service", "service_name", "status", "status_code", "duration_ms", "duration_ns", "start_ns", "end_ns", "start_time_ns", "end_time_ns", "trace_id"]);
  const extra = Object.entries(r).filter(([k, v]) => !used.has(k) && v != null && typeof v !== "object");
  return (
    <>
      <li className={`span-row${focusId === n.id ? " is-focus" : ""}`} style={{ paddingLeft: depth * 18 }}>
        <div className="span-line">
          <span className="span-branch" aria-hidden="true">{depth ? "└" : ""}</span>
          {svc && <span className="chip chip-sm">{svc}</span>}
          <span className="span-name">{name}</span>
          {status && <span className={`pill ${bad ? "pill-err" : "pill-quiet"}`}>{status}</span>}
          <span className="span-dur">{ms != null ? fmtMs(ms) : ""}</span>
        </div>
        {ms != null && maxMs > 0 && (
          <div className="span-bar" aria-hidden="true"><span style={{ width: `${Math.max(1, Math.min(100, (ms / maxMs) * 100))}%` }} className={bad ? "bad" : ""} /></div>
        )}
        {extra.length > 0 && (
          <div className="span-extra">{extra.slice(0, 6).map(([k, v]) => <span key={k}><code>{k}</code> {String(v)}</span>)}</div>
        )}
      </li>
      {n.children.map((c) => <SpanRow key={c.id} n={c} depth={depth + 1} maxMs={maxMs} focusId={focusId} />)}
    </>
  );
}

export default function EvidenceDrawer({ evidenceRef, demo, onClose }: { evidenceRef: string | null; demo: boolean; onClose: () => void }) {
  const [state, setState] = useState<State>({ s: "idle" });
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!evidenceRef) {
      setState({ s: "idle" });
      return;
    }
    if (demo) {
      setState({ s: "demo" });
      return;
    }
    let cancelled = false;
    setState({ s: "loading" });
    getEvidence(evidenceRef)
      .then((data) => !cancelled && setState({ s: "ok", data }))
      .catch((e: unknown) => !cancelled && setState({ s: "error", msg: e instanceof Error ? e.message : String(e) }));
    return () => {
      cancelled = true;
    };
  }, [evidenceRef, demo]);

  useEffect(() => {
    if (!evidenceRef) return;
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [evidenceRef, onClose]);

  const tree = useMemo(() => (state.s === "ok" && state.data.type === "trace" ? buildTree(state.data.spans ?? []) : []), [state]);
  const maxMs = useMemo(() => {
    if (state.s !== "ok" || state.data.type !== "trace") return 0;
    return Math.max(0, ...(state.data.spans ?? []).map((s) => spanMs(s) ?? 0));
  }, [state]);
  const focusId = evidenceRef?.includes("/span:") ? evidenceRef.split("/span:")[1] : null;

  if (!evidenceRef) return null;
  const resolvable = isResolvable(evidenceRef);

  return (
    <>
      <div className="scrim" onClick={onClose} aria-hidden="true" />
      <aside className="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-h">
        <header className="drawer-head">
          <div>
            <h2 id="drawer-h">Evidence</h2>
            <code className="ref-code">{evidenceRef}</code>
          </div>
          <button ref={closeRef} className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close evidence panel"><IconX /></button>
        </header>
        <div className="drawer-body">
          {state.s === "loading" && <p className="muted" role="status">Loading raw telemetry...</p>}
          {state.s === "demo" && (
            <div className="notice">
              <strong>Raw telemetry is available when connected to the backend.</strong>
              <p className="small">
                {resolvable
                  ? "This is a sample report, so the underlying log record or trace cannot be fetched."
                  : "This reference points to a derived value rather than a raw log or trace, so there is no record to open even when connected."}
              </p>
            </div>
          )}
          {state.s === "error" && <div className="notice notice-err" role="alert"><strong>Could not load evidence.</strong><p className="small">{state.msg}</p></div>}
          {state.s === "ok" && state.data.type === "derived" && (
            <div className="notice"><strong>Derived value</strong><p className="small">{state.data.note || "No note was provided for this reference."}</p></div>
          )}
          {state.s === "ok" && state.data.type === "log" && (
            <dl className="kv">
              {Object.entries(state.data.record ?? {}).map(([k, v]) => (
                <div key={k}><dt>{k}</dt><dd>{typeof v === "object" ? <pre>{JSON.stringify(v, null, 2)}</pre> : String(v)}</dd></div>
              ))}
            </dl>
          )}
          {state.s === "ok" && state.data.type === "trace" && (
            <>
              <p className="muted small">{(state.data.spans ?? []).length} spans{focusId ? ", the cited span is highlighted" : ""}.</p>
              <ul className="span-tree">{tree.map((n) => <SpanRow key={n.id} n={n} depth={0} maxMs={maxMs} focusId={focusId} />)}</ul>
            </>
          )}
        </div>
      </aside>
    </>
  );
}
