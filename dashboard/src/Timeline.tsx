import { useMemo, useState } from "react";
import type { TimelineEntry } from "./types";
import { IconPath, kindIcon } from "./icons";
import { fmtRel } from "./util";

export default function Timeline({ entries, onOpen }: { entries: TimelineEntry[]; onOpen: (ref: string) => void }) {
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const sorted = useMemo(() => [...entries].sort((a, b) => a.ts_ns - b.ts_ns), [entries]);
  const services = useMemo(() => [...new Set(entries.map((e) => e.service))].sort(), [entries]);
  const shown = sorted.filter((e) => !hidden.has(e.service));

  const toggle = (s: string) =>
    setHidden((h) => {
      const n = new Set(h);
      if (n.has(s)) n.delete(s);
      else n.add(s);
      return n;
    });

  return (
    <section className="card" aria-labelledby="tl-h">
      <div className="card-head">
        <h2 id="tl-h">Evidence timeline</h2>
        <p className="sub">Relative to the start of the investigation window. {shown.length} of {entries.length} events shown.</p>
      </div>
      {services.length > 1 && (
        <div className="chips" role="group" aria-label="Filter by service">
          {services.map((s) => (
            <button key={s} className="chip chip-btn" aria-pressed={!hidden.has(s)} onClick={() => toggle(s)}>{s}</button>
          ))}
        </div>
      )}
      {entries.length === 0 ? (
        <p className="muted">No timeline events in this report.</p>
      ) : shown.length === 0 ? (
        <p className="muted">All services are filtered out.</p>
      ) : (
        <ol className="timeline">
          {shown.map((e, i) => (
            <li key={`${e.ts_ns}-${e.ref}-${i}`} className={`tl-item${e.on_path ? " on-path" : ""}`}>
              <span className="tl-time">{fmtRel(e.t_rel_s)}</span>
              <span className={`tl-dot kind-${e.kind}`} aria-hidden="true">{kindIcon(e.kind)}</span>
              <div className="tl-body">
                <div className="tl-meta">
                  <span className="chip chip-sm">{e.service}</span>
                  <span className="kind-label">{e.kind}</span>
                  {e.on_path && <span className="pill pill-path"><IconPath />on propagation path</span>}
                </div>
                <p className="tl-sum">{e.summary}</p>
                {e.ref && <button className="ref-btn" onClick={() => onOpen(e.ref!)}>{e.ref}</button>}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
