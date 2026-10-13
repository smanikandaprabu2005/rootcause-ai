import { useMemo, useState } from "react";
import type { Anomaly, GraphEdge, Report } from "./types";
import { fmtMs, fmtNum, pct } from "./util";

const NODE_W = 156;
const NODE_H = 54;
const COL_W = 310;
const ROW_H = 104;
const PAD_X = 28;
const PAD_Y = 40;

type NodeState = "root" | "anomalous" | "starved" | "healthy";

interface Placed {
  id: string;
  x: number;
  y: number;
  layer: number;
  state: NodeState;
  anomalous: boolean;
  starved: boolean;
}

interface PlacedEdge {
  e: GraphEdge;
  d: string;
  lx: number;
  ly: number;
  onPath: boolean;
  tone: "err" | "warn" | "ok";
  width: number;
  x2: number;
  y2: number;
}

function layout(report: Report, rootId: string | null) {
  const ids = new Set<string>(report.graph.nodes.map((n) => n.id));
  report.graph.edges.forEach((e) => {
    ids.add(e.caller);
    ids.add(e.callee);
  });
  const all = [...ids].sort();
  const n = all.length;

  // Longest-path layering from entry points; bounded passes keep cycles finite.
  const layer: Record<string, number> = Object.fromEntries(all.map((id) => [id, 0]));
  for (let pass = 0; pass < n; pass++) {
    let changed = false;
    for (const e of report.graph.edges) {
      if (e.caller === e.callee) continue;
      const want = Math.min(layer[e.caller] + 1, n - 1);
      if (layer[e.callee] < want) {
        layer[e.callee] = want;
        changed = true;
      }
    }
    if (!changed) break;
  }

  const maxLayer = Math.max(0, ...Object.values(layer));
  const cols: string[][] = Array.from({ length: maxLayer + 1 }, () => []);
  all.forEach((id) => cols[layer[id]].push(id));

  // Order within each layer by the mean position of callers (reduces crossings).
  const order: Record<string, number> = {};
  cols[0].forEach((id, i) => (order[id] = i));
  for (let sweep = 0; sweep < 2; sweep++) {
    for (let c = 1; c <= maxLayer; c++) {
      const bary = (id: string) => {
        const ps = report.graph.edges.filter((e) => e.callee === id && layer[e.caller] < c).map((e) => order[e.caller] ?? 0);
        return ps.length ? ps.reduce((a, b) => a + b, 0) / ps.length : 0;
      };
      cols[c].sort((a, b) => bary(a) - bary(b) || a.localeCompare(b));
      cols[c].forEach((id, i) => (order[id] = i));
    }
  }

  const maxRows = Math.max(...cols.map((c) => c.length));
  const height = PAD_Y * 2 + (maxRows - 1) * ROW_H + NODE_H;
  const width = PAD_X * 2 + maxLayer * COL_W + NODE_W;
  const nodeInfo = new Map(report.graph.nodes.map((nn) => [nn.id, nn]));
  const roots = new Set([...report.root_candidates, ...(rootId ? [rootId] : [])]);

  const placed: Record<string, Placed> = {};
  cols.forEach((col, c) => {
    const colH = (col.length - 1) * ROW_H;
    col.forEach((id, i) => {
      const info = nodeInfo.get(id);
      const anomalous = !!info?.anomalous;
      const starved = !!info?.starved;
      const state: NodeState = roots.has(id) ? "root" : anomalous ? "anomalous" : starved ? "starved" : "healthy";
      placed[id] = {
        id,
        layer: c,
        x: PAD_X + c * COL_W,
        y: PAD_Y + (maxRows - 1) * ROW_H / 2 - colH / 2 + i * ROW_H,
        state,
        anomalous,
        starved,
      };
    });
  });

  const path = report.propagation_path;
  const pathEdges = new Set<string>();
  for (let i = 0; i + 1 < path.length; i++) pathEdges.add(`${path[i + 1]}>${path[i]}`);

  const maxCalls = Math.max(1, ...report.graph.edges.map((e) => e.calls || 0));
  const edges: PlacedEdge[] = report.graph.edges
    .filter((e) => placed[e.caller] && placed[e.callee])
    .map((e) => {
      const a = placed[e.caller];
      const b = placed[e.callee];
      const x1 = a.x + NODE_W;
      const y1 = a.y + NODE_H / 2;
      const x2 = b.x - 4;
      const y2 = b.y + NODE_H / 2;
      const dx = Math.max(40, (x2 - x1) / 2);
      // Edges that skip layers bow away from nodes sitting in the layers between.
      let bow = 0;
      const between = Object.values(placed).filter((p) => p.layer > a.layer && p.layer < b.layer);
      if (between.length) {
        const clear = (bw: number) => {
          const midY = (y1 + y2) / 2 + 0.75 * bw;
          return between.every((p) => Math.abs(p.y + NODE_H / 2 - midY) > NODE_H / 2 + 26);
        };
        const tries = [0, -50, 50, -100, 100, -150, 150, -200, 200];
        bow = tries.find(clear) ?? 0;
      }
      const d = `M${x1},${y1} C${x1 + dx},${y1 + bow} ${x2 - dx},${y2 + bow} ${x2},${y2}`;
      const tone: PlacedEdge["tone"] = e.error_rate >= 0.5 ? "err" : e.error_rate > 0 ? "warn" : "ok";
      return {
        e,
        d,
        lx: (x1 + x2) / 2,
        ly: (y1 + y2) / 2 + 0.75 * bow,
        onPath: pathEdges.has(`${e.caller}>${e.callee}`),
        tone,
        width: 1.5 + 3.5 * Math.sqrt((e.calls || 0) / maxCalls),
        x2,
        y2,
      };
    });

  return { placed: Object.values(placed), byId: placed, edges, width, height };
}

function stateWords(s: NodeState): string {
  return s === "root"
    ? "root-cause candidate"
    : s === "anomalous"
      ? "anomalous"
      : s === "starved"
        ? "starved, receiving less traffic than usual (likely a victim)"
        : "healthy";
}

export default function DependencyGraph({ report }: { report: Report }) {
  const [active, setActive] = useState<string | null>(null);
  const L = useMemo(() => layout(report, report.diagnosis.service), [report]);
  const anomalyOf = useMemo(() => {
    const m = new Map<string, Anomaly>();
    report.anomalies.forEach((a) => m.set(a.service, a));
    return m;
  }, [report.anomalies]);

  const summary = useMemo(() => {
    const parts = L.placed.map((p) => `${p.id} (${p.state})`).join(", ");
    const path = report.propagation_path.length ? ` Failure propagation path from root to entry point: ${report.propagation_path.join(" to ")}.` : "";
    const errEdges = L.edges
      .filter((x) => x.e.error_rate > 0)
      .map((x) => `${x.e.caller} to ${x.e.callee} ${pct(x.e.error_rate)} errors`)
      .join("; ");
    return `Dependency graph with ${L.placed.length} services and ${L.edges.length} calls. Services: ${parts}.${path}${errEdges ? ` Edges with errors: ${errEdges}.` : ""}`;
  }, [L, report.propagation_path]);

  const activeNode = active ? L.byId[active] : null;
  const activeAnom = active ? anomalyOf.get(active) : undefined;
  const pathIdx = (id: string) => report.propagation_path.indexOf(id);

  return (
    <section className="card" aria-labelledby="graph-h">
      <div className="card-head">
        <h2 id="graph-h">Dependency graph</h2>
        <p className="sub">Callers on the left, callees on the right. Hover or focus a service for its anomaly details.</p>
      </div>

      {L.placed.length === 0 ? (
        <p className="muted">No service graph in this report.</p>
      ) : (
        <>
          <p className="graph-hint" aria-hidden="true">Swipe sideways to see the whole graph →</p>
          <div className="graph-scroll" tabIndex={0} role="region" aria-label="Dependency graph (scrolls sideways on narrow screens)">
            <svg
              className="graph"
              viewBox={`0 0 ${L.width} ${L.height}`}
              style={{ minWidth: Math.min(L.width, 640), aspectRatio: `${L.width} / ${L.height}` }}
              role="group"
              aria-label={summary}
            >
              <defs>
                {(["err", "warn", "ok"] as const).map((t) => (
                  <marker key={t} id={`arrow-${t}`} viewBox="0 0 10 10" refX="9" refY="5" markerUnits="userSpaceOnUse" markerWidth="13" markerHeight="13" orient="auto-start-reverse">
                    <path d="M0,0 L10,5 L0,10 z" className={`arrow arrow-${t}`} />
                  </marker>
                ))}
              </defs>

              {L.edges.filter((x) => x.onPath).map((x) => (
                <path key={`halo-${x.e.caller}-${x.e.callee}`} d={x.d} className="edge-halo" strokeWidth={x.width + 10} />
              ))}
              {L.edges.map((x) => (
                <path
                  key={`${x.e.caller}-${x.e.callee}`}
                  d={x.d}
                  className={`edge edge-${x.tone}`}
                  strokeWidth={x.width}
                  markerEnd={`url(#arrow-${x.tone})`}
                  strokeDasharray={x.tone === "warn" ? "6 4" : undefined}
                />
              ))}
              {L.edges.map((x) => {
                const l1 = x.e.error_rate > 0 ? `${pct(x.e.error_rate)} err` : "0% err";
                const l2 = `p95 ${fmtMs(x.e.p95_ms)} · ${x.e.calls} calls`;
                const w = Math.max(l1.length * 7.6, l2.length * 6.3) + 14;
                return (
                  <g key={`lbl-${x.e.caller}-${x.e.callee}`} className="edge-label" transform={`translate(${x.lx},${x.ly})`}>
                    <rect x={-w / 2} y={-19} width={w} height={38} rx={6} className={`lbl-box lbl-${x.tone}`} />
                    <text y={-4} textAnchor="middle" className={`lbl-1 lbl-1-${x.tone}`}>{l1}</text>
                    <text y={12} textAnchor="middle" className="lbl-2">{l2}</text>
                  </g>
                );
              })}

              {L.placed.map((p) => {
                const pi = pathIdx(p.id);
                const tag = p.state === "root" ? "ROOT CANDIDATE" : p.state === "anomalous" ? "ANOMALOUS" : p.state === "starved" ? "STARVED" : "HEALTHY";
                return (
                  <g
                    key={p.id}
                    className={`node node-${p.state}${active === p.id ? " is-active" : ""}`}
                    transform={`translate(${p.x},${p.y})`}
                    tabIndex={0}
                    role="img"
                    aria-label={`${p.id}: ${stateWords(p.state)}${pi >= 0 ? `, step ${pi + 1} of ${report.propagation_path.length} on the propagation path` : ""}`}
                    onMouseEnter={() => setActive(p.id)}
                    onMouseLeave={() => setActive((a) => (a === p.id ? null : a))}
                    onFocus={() => setActive(p.id)}
                    onBlur={() => setActive((a) => (a === p.id ? null : a))}
                  >
                    {pi >= 0 && <rect x={-5} y={-5} width={NODE_W + 10} height={NODE_H + 10} rx={13} className="node-path-ring" />}
                    {p.state === "root" && <rect x={-3} y={-3} width={NODE_W + 6} height={NODE_H + 6} rx={11} className="node-root-ring" />}
                    <rect width={NODE_W} height={NODE_H} rx={9} className="node-body" />
                    <text x={NODE_W / 2} y={23} textAnchor="middle" className="node-name">{p.id}</text>
                    <text x={NODE_W / 2} y={41} textAnchor="middle" className="node-tag">{tag}</text>
                    {pi >= 0 && (
                      <g transform={`translate(${NODE_W - 4},-6)`}>
                        <circle r={10} className="step-dot" />
                        <text y={4} textAnchor="middle" className="step-num">{pi + 1}</text>
                      </g>
                    )}
                  </g>
                );
              })}
            </svg>
          </div>

          <div className="node-detail" aria-live="polite">
            {activeNode ? (
              <>
                <strong>{activeNode.id}</strong> <span className={`pill pill-${activeNode.state}`}>{stateWords(activeNode.state).split(",")[0]}</span>
                {activeAnom ? (
                  <>
                    <p className="muted small">
                      {activeAnom.anomalous_buckets ?? "?"} of {activeAnom.total_buckets ?? "?"} time buckets anomalous
                      {activeAnom.severity != null ? `, severity ${fmtNum(activeAnom.severity)}` : ""}.
                    </p>
                    <ul className="dev-list">
                      {Object.entries(activeAnom.deviating ?? {}).map(([f, v]) => (
                        <li key={f}>
                          <code>{f}</code>
                          <span>{fmtNum(v.baseline)} <span aria-label="to">→</span> <strong>{fmtNum(v.worst)}</strong></span>
                        </li>
                      ))}
                    </ul>
                  </>
                ) : (
                  <p className="muted small">No anomaly recorded for this service.</p>
                )}
              </>
            ) : (
              <p className="muted small">Hover or focus a service to see its baseline-to-worst feature deviations.</p>
            )}
          </div>

          <ul className="legend" aria-label="Graph legend">
            <li><svg width="26" height="16" aria-hidden="true"><rect x="2" y="2" width="22" height="12" rx="4" className="lg-root" /></svg>Root-cause candidate</li>
            <li><svg width="26" height="16" aria-hidden="true"><rect x="2" y="2" width="22" height="12" rx="4" className="lg-anom" /></svg>Anomalous</li>
            <li><svg width="26" height="16" aria-hidden="true"><rect x="2" y="2" width="22" height="12" rx="4" className="lg-starved" /></svg>Starved (victim: less traffic)</li>
            <li><svg width="26" height="16" aria-hidden="true"><rect x="2" y="2" width="22" height="12" rx="4" className="lg-healthy" /></svg>Healthy</li>
            <li><svg width="30" height="16" aria-hidden="true"><path d="M2 8h26" className="lg-halo" /><path d="M2 8h26" className="lg-err" /></svg>Propagation path (numbered root first)</li>
            <li><svg width="30" height="16" aria-hidden="true"><path d="M2 8h26" className="lg-err" /></svg>Error rate 50% or more</li>
            <li><svg width="30" height="16" aria-hidden="true"><path d="M2 8h26" className="lg-warn" strokeDasharray="5 3" /></svg>Some errors</li>
            <li><svg width="30" height="16" aria-hidden="true"><path d="M2 8h26" className="lg-ok" /></svg>No errors; thicker = more calls</li>
          </ul>
        </>
      )}
    </section>
  );
}
